"""Training cube v1: Zarr v3 stores laid out for streaming basin blocks from S3 or local NVMe.

Two physically separate stores per subset, so training jobs never download the frozen test years:

* `trainval.zarr`: 2000-01-01 to 2022-09-30 (train WY2001-2019, validation WY2020-2022; the first nine
  months are spin-up for lookback windows).
* `test.zarr`: 2022-06-01 to 2026-09-30 (frozen test WY2023-2026; June-September 2022 is lookback context
  only and duplicates the end of trainval).

Layout (xarray-compatible, one array per variable, NaN = missing):

* hourly `(basin, time)` arrays: targets, gauged outflow, and each forcing product's variables;
  `(basin, band, time)` for AORC elevation-band variables;
* forecasts `(basin, hrrr_init, hrrr_lead)` and `(basin, gefs_init, gefs_member, gefs_lead)`;
* statics `(basin,)`, `(basin, band)`, `(basin, band, month)`, plus `static_all (basin, attribute)`.

Every time-varying array has one inner chunk per basin covering the whole store period (one ranged read
loads a basin's full series), grouped 16 basins per shard object to keep object counts low. Blosc-zstd
with bit-shuffle keeps reads fast. Arrays carry `units`, `source`, and train-split `train_mean` /
`train_std` attributes.
"""

import hashlib
import json
import logging
import subprocess
from pathlib import Path

import boto3
import icechunk
import numpy as np
import pandas as pd
import scipy.sparse as sp
import xarray as xr
import zarr
from zarr.codecs import BloscCodec, BloscShuffle

from . import camelsh, config, extract
from .sources import SOURCES
from .targets import load_usgs

log = logging.getLogger(__name__)

SHARD_BASINS = 16
N_BANDS = 4
BAND_VARS = ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "sw_down_wm2")
PREFIX = {"aorc": "aorc", "hrrr_analysis": "hrrr_an", "mrms": "mrms", "hrrr_forecast": "hrrr_fc", "gefs_forecast": "gefs"}
UNITS = {
    "precip_mm_h": "mm/h", "temp_2m_c": "degC", "dewpoint_2m_c": "degC", "pressure_kpa": "kPa",
    "wind_speed_10m": "m/s", "sw_down_wm2": "W/m2", "lw_down_wm2": "W/m2", "spfh_2m_gkg": "g/kg",
}
CODEC = BloscCodec(cname="zstd", clevel=3, shuffle=BloscShuffle.bitshuffle)
TW_RANGE_C = (-1.0, 40.0)
HOURS_EPOCH = pd.Timestamp("2000-01-01T00:00")


# ------------------------------------------------------------------------------------------ inputs


def camelsh_discharge(root: Path, site: str) -> pd.Series:
    path = root.parent / "camelsh" / "ts" / "Data" / "CAMELSH" / "timeseries" / f"{site}.nc"
    if not path.exists():
        return pd.Series(dtype=np.float32)
    with xr.open_dataset(path) as ds:
        s = ds["Streamflow"].to_series().astype(np.float32)
    s.index = pd.DatetimeIndex(s.index).tz_localize("UTC")
    return s


def discharge_series(root: Path, site: str, index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Hourly discharge (m3/s) and source flag (0 missing, 1 CAMELSH, 2 USGS API) on `index`."""
    usgs = load_usgs(root / "targets", site, "discharge")["value"].astype(np.float32)
    cam = camelsh_discharge(root, site)
    usgs_from = config.USGS_TARGETS_FROM if len(cam) else pd.Timestamp.min.tz_localize("UTC")
    q = pd.Series(np.nan, index=index, dtype=np.float32)
    src = pd.Series(0, index=index, dtype=np.int8)
    if len(cam):
        c = cam[cam.index < usgs_from].reindex(index)
        q = q.where(c.isna(), c)
        src[c.notna()] = 1
    u = usgs[usgs.index >= usgs_from].reindex(index)
    q = q.where(u.isna(), u)
    src[u.notna()] = 2
    q[q < 0] = np.nan
    src[q.isna()] = 0
    return q.to_numpy(np.float32), src.to_numpy(np.int8)


def temperature_series(root: Path, site: str, index: pd.DatetimeIndex) -> np.ndarray:
    tw = load_usgs(root / "targets", site, "water_temperature")["value"].astype(np.float32).reindex(index)
    tw[(tw < TW_RANGE_C[0]) | (tw > TW_RANGE_C[1])] = np.nan
    return tw.to_numpy(np.float32)


def overlap_qa(root: Path, sites: list[str]) -> dict:
    """CAMELSH vs USGS API discharge in 2024 (both sources overlap there) as a consistency check."""
    ratios = []
    for s in sites:
        cam = camelsh_discharge(root, s)
        usgs = load_usgs(root / "targets", s, "discharge")["value"]
        both = pd.concat([cam.rename("c"), usgs.rename("u")], axis=1).dropna()
        both = both[(both.index >= "2024-01-01") & (both["u"] > 0)]
        if len(both) > 1000:
            ratios.append(float((both["c"] / both["u"]).median()))
    r = np.array(ratios)
    return {"sites": len(r), "median_ratio": float(np.median(r)) if len(r) else None, "frac_within_2pct": float(np.mean(np.abs(r - 1) < 0.02)) if len(r) else None}


def static_tables(root: Path, basins: list[str]) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    """Per-basin numeric attributes, and band-level terrain arrays."""
    attrs = camelsh.attributes(root.parent / "camelsh").loc[basins]
    numeric = attrs.select_dtypes("number").astype(np.float64)
    reg = pd.read_parquet(root / "regulation.parquet").loc[basins].select_dtypes("number")
    sel = pd.read_parquet(root / "selection.parquet").loc[basins]

    ter = np.load(root / "aorc_cell_terrain.npz")
    cells, layers = ter["cells"], ter["layers"]
    w_all = sp.load_npz(root / "weights_aorc_all.npz").tocsr()
    all_units = json.loads((root / "aorc_units.json").read_text())
    pos = {u: i for i, u in enumerate(all_units)}
    lookup = np.full(w_all.shape[1], -1)
    lookup[cells] = np.arange(len(cells))

    def unit_means(unit: str) -> np.ndarray:
        row = w_all[pos[unit]]
        vals = layers[:, lookup[row.indices]]
        return (vals * row.data).sum(axis=1) / row.data.sum()

    names = ["elev_m", "slope_deg", "northness", "eastness", "sky_view", *[f"sw_factor_m{m:02d}" for m in range(1, 13)]]
    basin_t = np.stack([unit_means(b) for b in basins])
    band_t = np.stack([[unit_means(f"{b}/band{k}") for k in range(N_BANDS)] for b in basins])
    band_frac = np.stack([[w_all[pos[f"{b}/band{k}"]].data.sum() / w_all[pos[b]].data.sum() for k in range(N_BANDS)] for b in basins])

    core = pd.DataFrame(
        {
            "area_km2": numeric["DRAIN_SQKM"],
            "log_area": np.log(numeric["DRAIN_SQKM"]),
            "lat": numeric["LAT_GAGE"],
            "lon": numeric["LNG_GAGE"],
            "elevation_m": basin_t[:, 0],
            "slope_deg": basin_t[:, 1],
            "northness": basin_t[:, 2],
            "eastness": basin_t[:, 3],
            "sky_view": basin_t[:, 4],
            "p_mean": numeric["p_mean"],
            "pet_mean": numeric["pet_mean"],
            "aridity": numeric["aridity_index"],
            "p_seasonality": numeric["p_seasonality"],
            "frac_snow": numeric["frac_snow"],
            "high_prec_freq": numeric["high_prec_freq"],
            "high_prec_dur": numeric["high_prec_dur"],
            "low_prec_freq": numeric["low_prec_freq"],
            "low_prec_dur": numeric["low_prec_dur"],
            "t_mean": numeric["T_AVG_BASIN"],
            "baseflow_index": numeric["BFI_AVE"] / 100.0,
            "forest_frac": numeric["FORESTNLCD06"] / 100.0,
            "developed_frac": numeric["DEVNLCD06"] / 100.0,
            "runoff_mean_mm_yr": numeric["RUNAVE7100"],
        },
        index=basins,
    )
    core = core.join(reg)
    core["has_temperature"] = sel["has_temperature"].astype(float)
    band = {
        "band_area_frac": band_frac,
        "band_elev_m": band_t[:, :, 0],
        "band_slope_deg": band_t[:, :, 1],
        "band_northness": band_t[:, :, 2],
        "band_eastness": band_t[:, :, 3],
        "band_sky_view": band_t[:, :, 4],
        "band_sw_factor": band_t[:, :, 5:],
        "terrain_sw_factor": basin_t[:, 5:],
    }
    everything = numeric.add_prefix("gagesii_").join(core)
    everything = everything.loc[:, ~everything.columns.duplicated()]
    return core, {"band": band, "all": everything}


# ------------------------------------------------------------------------------------------ writer


def _empty(group: zarr.Group, name: str, shape: tuple[int, ...], dtype, dims: tuple[str, ...], attrs: dict, basin_axis: bool = True) -> zarr.Array:
    if basin_axis and len(shape) > 1:
        chunks, shards = (1, *shape[1:]), (SHARD_BASINS, *shape[1:])
    else:
        chunks, shards = shape, None
    chunks = tuple(max(c, 1) for c in chunks)
    shards = None if shards is None else tuple(max(c, 1) for c in shards)
    dtype = np.dtype(dtype)
    fill = np.nan if dtype.kind == "f" else (0 if dtype.kind in "iu" else "")
    arr = group.create_array(
        name, shape=shape, dtype=dtype, chunks=chunks, shards=shards, compressors=[CODEC] if dtype.kind in "fiu" else None,
        dimension_names=dims, fill_value=fill, overwrite=True,
    )
    arr.attrs.update(attrs)
    return arr


def _create(group: zarr.Group, name: str, data: np.ndarray, dims: tuple[str, ...], attrs: dict, basin_axis: bool = True) -> None:
    _empty(group, name, data.shape, data.dtype, dims, attrs, basin_axis)[...] = data


class RunningStats:
    """Mean/std over finite values inside a time mask, accumulated block by block."""

    def __init__(self, mask: np.ndarray | None, axis: int):
        self.mask, self.axis = mask, axis
        self.fallback = mask is None or not mask.any()
        self.n = 0
        self.s = 0.0
        self.ss = 0.0

    def add(self, block: np.ndarray) -> None:
        sub = block if self.fallback else np.compress(self.mask, block, axis=self.axis)
        f = sub[np.isfinite(sub)].astype(np.float64)
        self.n += f.size
        self.s += f.sum()
        self.ss += (f * f).sum()

    def result(self) -> dict:
        period = "validation (no train-split data)" if self.fallback else "train"
        if self.n == 0:
            return {"train_mean": None, "train_std": None, "stats_period": period}
        mean = self.s / self.n
        return {"train_mean": mean, "train_std": float(np.sqrt(max(self.ss / self.n - mean * mean, 0.0))), "stats_period": period}


def _hours(times: pd.DatetimeIndex) -> np.ndarray:
    return ((times.tz_convert(None) - HOURS_EPOCH) / pd.Timedelta(hours=1)).to_numpy().astype(np.int64)


class ExtractReader:
    """Shard outputs of one source for one subset, fetched from S3 once and memory-mapped."""

    def __init__(self, root: Path, run: str, source: str, kind: str):
        self.source, self.kind = source, kind
        self.dir = root / "extract" / run / source
        self.shards: list[tuple[dict, np.ndarray]] = []
        for meta_path in sorted(self.dir.glob(f"*/{kind}.json")):
            meta = json.loads(meta_path.read_text())
            npy = meta_path.with_suffix(".npy")
            if not npy.exists():
                np.save(npy, extract.read_output(meta_path.with_suffix(".npy.zst")))
            self.shards.append((meta, np.load(npy, mmap_mode="r")))
        self.shards.sort(key=lambda s: s[0]["leading"][0])

    def series(self, units: list[str], index: pd.DatetimeIndex) -> np.ndarray:
        """(len(units), len(index), vars) for analysis sources."""
        nvar = len(SOURCES[self.source].outputs)
        out = np.full((len(units), len(index), nvar), np.nan, dtype=np.float32)
        for meta, arr in self.shards:
            pos = {u: i for i, u in enumerate(meta["units"])}
            t = pd.DatetimeIndex(pd.to_datetime(meta["leading"])).tz_localize("UTC")
            dst = index.get_indexer(t)
            ok = dst >= 0
            for j, u in enumerate(units):
                if u in pos:
                    out[j, dst[ok]] = arr[pos[u]][ok]
        return out

    def inits(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
        t = [pd.Timestamp(x, tz="UTC") for meta, _ in self.shards for x in meta["leading"]]
        idx = pd.DatetimeIndex(sorted(set(t)))
        return idx[(idx >= start) & (idx <= end)]

    def forecasts(self, units: list[str], inits: pd.DatetimeIndex) -> np.ndarray:
        src = SOURCES[self.source]
        lead_shape = (src.members, src.leads) if src.members else (src.leads,)
        out = np.full((len(units), len(inits), *lead_shape, len(src.outputs)), np.nan, dtype=np.float32)
        for meta, arr in self.shards:
            pos = {u: i for i, u in enumerate(meta["units"])}
            t = pd.DatetimeIndex(pd.to_datetime(meta["leading"])).tz_localize("UTC")
            dst = inits.get_indexer(t)
            ok = dst >= 0
            for j, u in enumerate(units):
                if u in pos:
                    out[j, dst[ok]] = arr[pos[u]][ok]
        return out

    def leads_h(self) -> list[float]:
        return self.shards[0][0]["leads_h"]


def download_extract(root: Path, run: str, kind: str) -> None:
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    prefix = f"work/runs/{run}/extract/"
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.startswith(kind + "."):
                continue
            dest = root / "extract" / run / obj["Key"][len(prefix):]
            if dest.exists() and dest.stat().st_size == obj["Size"]:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(config.BUCKET, obj["Key"], str(dest))


def write_store(path: Path, basins: list[str], store: str, root: Path, readers: dict[str, ExtractReader], subset: str) -> dict:
    start, end = config.STORES[store]
    index = config.hourly_index(start, end)
    train = next(s for s in config.SPLITS if s.name == "train")
    train_mask = np.asarray((index >= train.start) & (index <= train.end))
    outflows = json.loads((root / "outflows.json").read_text())
    core, extra = static_tables(root, basins)
    area = core["area_km2"].to_numpy()

    group = zarr.open_group(path, mode="w", zarr_format=3)
    _create(group, "basin", np.array(basins, dtype="<U8"), ("basin",), {}, basin_axis=False)
    _create(group, "time", _hours(index), ("time",), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}, basin_axis=False)
    _create(group, "band", np.arange(N_BANDS, dtype=np.int8), ("band",), {"description": "equal-area elevation bands, 0 = lowest"}, basin_axis=False)
    _create(group, "month", np.arange(1, 13, dtype=np.int8), ("month",), {}, basin_axis=False)

    # Targets and regulation input.
    q = np.full((len(basins), len(index)), np.nan, dtype=np.float32)
    qsrc = np.zeros((len(basins), len(index)), dtype=np.int8)
    tw = np.full_like(q, np.nan)
    outflow = np.full_like(q, np.nan)
    cache: dict[str, np.ndarray] = {}
    for i, b in enumerate(basins):
        q[i], qsrc[i] = discharge_series(root, b, index)
        tw[i] = temperature_series(root, b, index)
        gauges = outflows.get(b, [])
        if gauges:
            total = np.zeros(len(index), dtype=np.float32)
            for g in gauges:
                if g not in cache:
                    cache[g] = discharge_series(root, g, index)[0]
                total = total + cache[g]
            outflow[i] = total * 3.6 / area[i]
    qmm = q * 3.6 / area[:, None]
    per_basin_std = np.array([np.nanstd(r[train_mask]) if np.isfinite(r[train_mask]).any() else np.nan for r in qmm], dtype=np.float32)
    per_basin_mean = np.array([np.nanmean(r[train_mask]) if np.isfinite(r[train_mask]).any() else np.nan for r in qmm], dtype=np.float32)
    hourly = {
        "qobs_mm_h": (qmm, {"units": "mm/h", "source": "CAMELSH hourly (to 2023) + USGS Water Data API (2024 on, or full record)", "long_name": "specific discharge"}),
        "qobs_m3s": (q, {"units": "m3/s", "source": "as qobs_mm_h"}),
        "tw_c": (tw, {"units": "degC", "source": "USGS Water Data API 00010, hour-ending mean", "long_name": "water temperature"}),
        "gauged_outflow_mm_h": (outflow, {"units": "mm/h", "source": "sum of below-dam gauges upstream (outflows.json); NaN if none or any missing"}),
    }
    stats = {}
    for name, (data, attrs) in hourly.items():
        rs = RunningStats(train_mask, 1)
        rs.add(data)
        stats[name] = rs.result()
        _create(group, name, data, ("basin", "time"), {**attrs, **stats[name]})
    _create(group, "qobs_source", qsrc, ("basin", "time"), {"flag_values": [0, 1, 2], "flag_meanings": "missing camelsh usgs_api"})
    _create(group, "qobs_mm_h_train_std", per_basin_std, ("basin",), {"units": "mm/h"}, basin_axis=False)
    _create(group, "qobs_mm_h_train_mean", per_basin_mean, ("basin",), {"units": "mm/h"}, basin_axis=False)

    blocks = [(i, min(i + SHARD_BASINS, len(basins))) for i in range(0, len(basins), SHARD_BASINS)]

    # Analysis forcings, 16 basins (one shard) at a time.
    for source in ("aorc", "hrrr_analysis", "mrms"):
        reader = readers.get(source)
        if reader is None:
            continue
        src = SOURCES[source]
        names = [f"{PREFIX[source]}_{v}" for v in src.outputs]
        arrs = {n: _empty(group, n, (len(basins), len(index)), np.float32, ("basin", "time"), {"units": UNITS[v], "source": source}) for n, v in zip(names, src.outputs)}
        rstats = {n: RunningStats(train_mask, 1) for n in names}
        if source == "aorc":
            bnames = [f"aorc_band_{v}" for v in BAND_VARS]
            barrs = {n: _empty(group, n, (len(basins), N_BANDS, len(index)), np.float32, ("basin", "band", "time"), {"units": UNITS[v], "source": "aorc"}) for n, v in zip(bnames, BAND_VARS)}
            rstats |= {n: RunningStats(train_mask, 2) for n in bnames}
        for i0, i1 in blocks:
            data = reader.series(basins[i0:i1], index)
            for k, n in enumerate(names):
                arrs[n][i0:i1] = data[..., k]
                rstats[n].add(data[..., k])
            if source == "aorc":
                units = [f"{b}/band{k}" for b in basins[i0:i1] for k in range(N_BANDS)]
                bdata = reader.series(units, index).reshape(i1 - i0, N_BANDS, len(index), len(src.outputs))
                for n, v in zip(bnames, BAND_VARS):
                    block = bdata[..., src.outputs.index(v)]
                    barrs[n][i0:i1] = block
                    rstats[n].add(block)
        for n, rs in rstats.items():
            stats[n] = rs.result()
            group[n].attrs.update(stats[n])

    # Forecast forcings.
    for source, dim in (("hrrr_forecast", "hrrr"), ("gefs_forecast", "gefs")):
        reader = readers.get(source)
        if reader is None:
            continue
        src = SOURCES[source]
        inits = reader.inits(start, end)
        init_train = np.asarray((inits >= train.start) & (inits <= train.end))
        _create(group, f"{dim}_init", _hours(inits), (f"{dim}_init",), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}, basin_axis=False)
        _create(group, f"{dim}_lead", np.array(reader.leads_h(), dtype=np.float32), (f"{dim}_lead",), {"units": "hours"}, basin_axis=False)
        dims = ("basin", f"{dim}_init", f"{dim}_lead")
        shape = (len(basins), len(inits), src.leads)
        if src.members:
            _create(group, f"{dim}_member", np.arange(src.members, dtype=np.int8), (f"{dim}_member",), {"description": "0 = control"}, basin_axis=False)
            dims = ("basin", f"{dim}_init", f"{dim}_member", f"{dim}_lead")
            shape = (len(basins), len(inits), src.members, src.leads)
        names = [f"{PREFIX[source]}_{v}" for v in src.outputs]
        arrs = {n: _empty(group, n, shape, np.float32, dims, {"units": UNITS[v], "source": source}) for n, v in zip(names, src.outputs)}
        rstats = {n: RunningStats(init_train, 1) for n in names}
        for i0, i1 in blocks:
            data = reader.forecasts(basins[i0:i1], inits)
            for k, n in enumerate(names):
                arrs[n][i0:i1] = data[..., k]
                rstats[n].add(data[..., k])
        for n, rs in rstats.items():
            stats[n] = rs.result()
            group[n].attrs.update(stats[n])

    # Statics.
    for col in core.columns:
        _create(group, col, core[col].to_numpy(np.float32), ("basin",), {}, basin_axis=False)
    for name, arr in extra["band"].items():
        dims = ("basin", "band", "month") if arr.ndim == 3 else (("basin", "month") if name == "terrain_sw_factor" else ("basin", "band"))
        _create(group, name, arr.astype(np.float32), dims, {}, basin_axis=False)
    allt = extra["all"]
    _create(group, "attribute", np.array(allt.columns, dtype=f"<U{max(map(len, allt.columns))}"), ("attribute",), {}, basin_axis=False)
    _create(group, "static_all", allt.to_numpy(np.float32), ("basin", "attribute"), {"description": "all numeric GAGES-II, NLDAS climate, NID/regulation and terrain attributes"}, basin_axis=False)
    _create(group, "gauged_outflow_sites", np.array([",".join(outflows.get(b, [])) for b in basins], dtype="<U128"), ("basin",), {}, basin_axis=False)

    group.attrs.update(
        {
            "flowcast_schema": "flowcast-training-cube",
            "version": config.VERSION,
            "subset": subset,
            "store": store,
            "store_period": [str(start), str(end)],
            "splits": {s.name: [str(s.start), str(s.end)] for s in config.SPLITS},
            "target_unit": "mm/h",
            "area_attribute": "area_km2",
            "time_convention": "hour-ending: value at t covers (t-1h, t]; forecasts are indexed by init time and lead",
            "licenses": "USGS, NOAA, NID: public domain; CAMELSH CC-BY 4.0; Copernicus DEM GLO-90 (ESA/Copernicus licence)",
        }
    )
    zarr.consolidate_metadata(path)
    return stats


def source_provenance() -> dict:
    out = {"aorc": {"bucket": "noaa-nws-aorc-v1-1-1km", "version": "v1.1"}}
    for name, src in SOURCES.items():
        if not src.prefix:
            continue
        repo = icechunk.Repository.open(icechunk.s3_storage(bucket=src.bucket, prefix=src.prefix, region=src.region, anonymous=True))
        out[name] = {"icechunk": f"s3://{src.bucket}/{src.prefix}", "main_snapshot_at_assembly": repo.lookup_branch("main")}
    out["camelsh"] = "https://doi.org/10.5281/zenodo.15066778"
    out["nid"] = "https://nid.sec.usace.army.mil/api/nation/csv"
    out["usgs"] = "https://api.waterdata.usgs.gov/ogcapi/v0 (continuous 00060, 00010)"
    out["dem"] = "s3://copernicus-dem-90m (GLO-90)"
    return out


def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(path)).encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def assemble_cube(root: Path, run: str, subset: str, upload: bool) -> None:
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    early = json.loads((root / "slice.json").read_text())
    basins = early if subset == "slice50" else sel
    kind = "slice" if subset == "slice50" else "all"
    download_extract(root, run, kind)
    readers = {s: ExtractReader(root, run, s, kind) for s in SOURCES}
    readers = {s: r for s, r in readers.items() if r.shards}
    missing = sorted(set(SOURCES) - set(readers))
    out = root / "cube" / config.VERSION / subset
    manifest = {
        "version": config.VERSION,
        "subset": subset,
        "run": run,
        "created": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True, text=True).stdout.strip(),
        "basins": basins,
        "missing_sources": missing,
        "shards": {s: [m["shard"] for m, _ in r.shards] for s, r in readers.items()},
        "qa_camelsh_vs_usgs_2024": overlap_qa(root, basins),
        "sources": source_provenance(),
        "stores": {},
    }
    for store in config.STORES:
        path = out / f"{store}.zarr"
        stats = write_store(path, basins, store, root, readers, subset)
        manifest["stores"][store] = {"sha256": tree_hash(path), "bytes": sum(f.stat().st_size for f in path.rglob("*") if f.is_file()), "stats": stats}
        log.info("%s %s written: %.2f GB", subset, store, manifest["stores"][store]["bytes"] / 1e9)
    manifest_text = json.dumps(manifest, indent=1, default=str)
    (out / "manifest.json").write_text(manifest_text)
    manifest_id = hashlib.sha256(manifest_text.encode()).hexdigest()[:16]
    (out / "MANIFEST_ID").write_text(manifest_id + "\n")
    if upload:
        dest = f"s3://{config.BUCKET}/{config.VERSION}/{subset}/"
        subprocess.run(["aws", "s3", "sync", "--only-show-errors", "--delete", str(out) + "/", dest], check=True)
        log.info("uploaded %s (manifest %s)", dest, manifest_id)
