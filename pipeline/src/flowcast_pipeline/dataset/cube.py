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
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
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
# Physical bounds for forecast forcings; values outside become NaN. The GEFSv12 reforecast has at least one corrupt
# GRIB field (2004-03-05 00Z p03, 36-39 h precipitation: every cell > 1,000, up to 7e6).
FORCING_RANGES = {
    "precip_mm_h": (0.0, 300.0),
    "temp_2m_c": (-80.0, 60.0),
    "dewpoint_2m_c": (-90.0, 50.0),
    "pressure_kpa": (40.0, 110.0),
    "wind_speed_10m": (0.0, 100.0),
    "sw_down_wm2": (0.0, 1400.0),
    "lw_down_wm2": (50.0, 700.0),
}


def qc_range(x: np.ndarray, var: str) -> int:
    """Set out-of-range values to NaN in place; returns how many were removed."""
    lo, hi = FORCING_RANGES[var]
    with np.errstate(invalid="ignore"):
        bad = (x < lo) | (x > hi)
    n = int(bad.sum())
    if n:
        x[bad] = np.nan
    return n
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

    def __init__(self, root: Path, run: str, source: str, kind: str, keep_compressed: bool = True):
        self.source, self.kind = source, kind
        self.dir = root / "extract" / run / source
        self.shards: list[tuple[dict, np.ndarray]] = []
        metas = sorted(self.dir.glob(f"*/{kind}.json"))

        def decompress(meta_path: Path) -> None:
            npy = meta_path.with_suffix(".npy")
            if not npy.exists():
                tmp = npy.with_suffix(".tmp.npy")
                np.save(tmp, extract.read_output(meta_path.with_suffix(".npy.zst")))
                tmp.replace(npy)
                if not keep_compressed:
                    meta_path.with_suffix(".npy.zst").unlink()

        with ThreadPoolExecutor(4) as pool:
            list(pool.map(decompress, metas))
        for meta_path in metas:
            self.shards.append((json.loads(meta_path.read_text()), np.load(meta_path.with_suffix(".npy"), mmap_mode="r")))
        log.info("%s: %d %s shards ready", source, len(self.shards), kind)
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
        shape = self.shards[0][1].shape  # (units, inits, [members,] leads, vars)
        out = np.full((len(units), len(inits), *shape[2:]), np.nan, dtype=np.float32)
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
    fetched = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.startswith(kind + "."):
                continue
            dest = root / "extract" / run / obj["Key"][len(prefix):]
            if (dest.exists() and dest.stat().st_size == obj["Size"]) or dest.with_suffix("").with_suffix(".npy").exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(config.BUCKET, obj["Key"], str(dest))
            fetched += 1
            if fetched % 50 == 0:
                log.info("downloaded %d extraction files", fetched)


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


def git_sha() -> str:
    """HEAD of the checkout, or the code bundle's SHA on instances (which have neither git nor a .git directory)."""
    if shutil.which("git"):
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True, text=True).stdout.strip()
        if sha:
            return sha
    return os.environ.get("FLOWCAST_GIT_SHA", "")


def listing_hash(bucket: str, prefix: str) -> tuple[str, int]:
    """SHA-256 over (key, ETag, size) of every object under a prefix, and total bytes (content hashes need a full read)."""
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    h, total = hashlib.sha256(), 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            h.update(f"{obj['Key'][len(prefix):]}|{obj['ETag']}|{obj['Size']}\n".encode())
            total += obj["Size"]
    return h.hexdigest(), total


def _write_forecast_block_arrays(
    group: zarr.Group, reader: ExtractReader, basins: list[str], inits: pd.DatetimeIndex, prefix: str, dim: str,
    train_mask: np.ndarray, band_vars: tuple[str, ...], basin_vars: bool,
) -> dict:
    """Write `{prefix}_{var}` (basin, init, member, lead) and `{prefix}_band_{var}` (basin, init, member, lead, band)."""
    shape = reader.shards[0][1].shape
    members, leads = shape[2], shape[3]
    outputs = json.loads(next(iter(reader.dir.glob("*/all.json"))).read_text())["variables"]
    dims = ("basin", f"{dim}_init", f"{dim}_member", f"{dim}_lead")
    arrs, stats = {}, {}
    if basin_vars:
        for v in outputs:
            arrs[f"{prefix}_{v}"] = _empty(group, f"{prefix}_{v}", (len(basins), len(inits), members, leads), np.float32, dims, {"units": UNITS[v], "source": reader.source})
    for v in band_vars:
        arrs[f"{prefix}_band_{v}"] = _empty(
            group, f"{prefix}_band_{v}", (len(basins), len(inits), members, leads, N_BANDS), np.float32, (*dims, "band"),
            {"units": UNITS[v], "source": reader.source, "description": "elevation bands last (0 = lowest)"},
        )
    rstats = {n: RunningStats(train_mask, 1) for n in arrs}
    removed = {n: 0 for n in arrs}
    for i0 in range(0, len(basins), SHARD_BASINS):
        block = basins[i0 : i0 + SHARD_BASINS]
        if basin_vars:
            data = reader.forecasts(block, inits)
            for k, v in enumerate(outputs):
                removed[f"{prefix}_{v}"] += qc_range(data[..., k], v)
                arrs[f"{prefix}_{v}"][i0 : i0 + len(block)] = data[..., k]
                rstats[f"{prefix}_{v}"].add(data[..., k])
        if band_vars:
            units = [f"{b}/band{k}" for b in block for k in range(N_BANDS)]
            bdata = reader.forecasts(units, inits).reshape(len(block), N_BANDS, len(inits), members, leads, len(outputs))
            for v in band_vars:
                x = np.moveaxis(bdata[..., outputs.index(v)], 1, -1)
                removed[f"{prefix}_band_{v}"] += qc_range(x, v)
                arrs[f"{prefix}_band_{v}"][i0 : i0 + len(block)] = x
                rstats[f"{prefix}_band_{v}"].add(x)
        log.info("%s: basins %d-%d written", prefix, i0, i0 + len(block))
    for n, rs in rstats.items():
        var = n.split("_", 2)[-1].removeprefix("band_")
        stats[n] = rs.result() | {"qc_values_removed": removed[n], "qc_range": list(FORCING_RANGES[var])}
        group[n].attrs.update(stats[n])
    return stats


V11_PREFIX = os.environ.get("FLOWCAST_V11_PREFIX", "v1.1")


def add_v11(root: Path, run: str, subset: str) -> str:
    """v1.1 = the v1 stores plus the GEFSv12 reforecast (2000-2019) and elevation-band operational GEFS.

    The v1 subset is copied server-side to `v1.1/<subset>/`, then new arrays are appended in place in S3.
    Existing v1 arrays are untouched, so v1-based loaders keep working on v1.1.
    """
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    basins = json.loads((root / "slice.json").read_text()) if subset == "slice50" else sel
    download_extract(root, run, "all")
    rf = ExtractReader(root, run, "gefs_reforecast", "all", keep_compressed=False)
    ob = ExtractReader(root, run, "gefs_forecast_bands", "all", keep_compressed=False)
    src, dst = f"s3://{config.BUCKET}/v1/{subset}/", f"s3://{config.BUCKET}/{V11_PREFIX}/{subset}/"
    subprocess.run(["aws", "s3", "sync", "--only-show-errors", "--delete", src, dst], check=True)
    base_manifest = json.loads(subprocess.run(["aws", "s3", "cp", src + "manifest.json", "-"], capture_output=True, text=True, check=True).stdout)
    base_id = subprocess.run(["aws", "s3", "cp", src + "MANIFEST_ID", "-"], capture_output=True, text=True, check=True).stdout.strip()
    train = next(s for s in config.SPLITS if s.name == "train")
    manifest = {
        "version": "v1.1",
        "base": {"version": config.VERSION, "manifest_id": base_id},
        "subset": subset,
        "run": run,
        "created": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_sha": git_sha(),
        "basins": basins,
        "additions": {},
        "reforecast": {
            "source": "s3://noaa-gefs-retrospective/GEFSv12/reforecast (Days:1-10, 0.25 degree)",
            "members": ["c00", "p01", "p02", "p03", "p04"],
            "shards": len(rf.shards),
            "missing_member_runs": int(sum(json.loads(p.read_text()).get("missing_members", 0) for p in rf.dir.glob("*/all.json"))),
        },
        "stores": {},
        "base_manifest": base_manifest,
    }
    for store, (start, end) in config.STORES.items():
        group = zarr.open_group(f"{dst}{store}.zarr", mode="r+", use_consolidated=False, storage_options={"anon": False})
        stats = {}
        op_inits = pd.to_datetime(group["gefs_init"][:], unit="h", origin=HOURS_EPOCH).tz_localize("UTC")
        # The band extraction ran later and may include newer inits; the store's existing axis is what counts.
        missing = op_inits.difference(ob.inits(start, end))
        if len(missing):
            raise ValueError(f"{store}: band extraction lacks {len(missing)} operational GEFS inits, e.g. {missing[0]}")
        op_train = np.asarray((op_inits >= train.start) & (op_inits <= train.end))
        stats |= _write_forecast_block_arrays(group, ob, basins, op_inits, "gefs", "gefs", op_train, BAND_VARS, basin_vars=False)
        rf_inits = rf.inits(start, end)
        if len(rf_inits):
            _create(group, "gefs_rf_init", _hours(rf_inits), ("gefs_rf_init",), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}, basin_axis=False)
            _create(group, "gefs_rf_lead", np.array(rf.leads_h(), dtype=np.float32), ("gefs_rf_lead",), {"units": "hours"}, basin_axis=False)
            _create(group, "gefs_rf_member", np.arange(rf.shards[0][1].shape[2], dtype=np.int8), ("gefs_rf_member",), {"description": "0 = control (c00), 1-4 = p01-p04"}, basin_axis=False)
            rf_train = np.asarray((rf_inits >= train.start) & (rf_inits <= train.end))
            stats |= _write_forecast_block_arrays(group, rf, basins, rf_inits, "gefs_rf", "gefs_rf", rf_train, BAND_VARS, basin_vars=True)
        group.attrs.update({"version": "v1.1", "v1_1_additions": sorted(stats)})
        zarr.consolidate_metadata(group.store)
        digest, nbytes = listing_hash(config.BUCKET, f"{V11_PREFIX}/{subset}/{store}.zarr/")
        manifest["stores"][store] = {"listing_sha256": digest, "bytes": nbytes, "stats": stats}
        manifest["additions"][store] = sorted(stats)
        log.info("v1.1 %s %s: %d arrays added, %.2f GB total", subset, store, len(stats), nbytes / 1e9)
    text = json.dumps(manifest, indent=1, default=str)
    manifest_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    s3.put_object(Bucket=config.BUCKET, Key=f"{V11_PREFIX}/{subset}/manifest.json", Body=text.encode())
    s3.put_object(Bucket=config.BUCKET, Key=f"{V11_PREFIX}/{subset}/MANIFEST_ID", Body=(manifest_id + "\n").encode())
    log.info("published %s (manifest %s)", dst, manifest_id)
    return manifest_id


V12_PREFIX = os.environ.get("FLOWCAST_V12_PREFIX", "v1.2")
N_UPSTREAM_SLOTS = 3


def upstream_arrays(root: Path, basins: list[str], areas: pd.Series, up: dict, index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Hourly upstream-gauge features for one store window.

    `upstream_gauged_q_mm_h`: summed flow of the basin's outermost (non-nested) upstream gauges with data at that
    hour, over the target basin's area. `upstream_gauged_frac`: the share of the target's area those gauges
    drain. `upstream_slot_q_mm_h`: the three largest outermost gauges individually, also over the target's area.
    """
    n = len(index)
    q_sum = np.zeros((len(basins), n), dtype=np.float32)
    frac = np.zeros((len(basins), n), dtype=np.float32)
    slot_q = np.full((len(basins), N_UPSTREAM_SLOTS, n), np.nan, dtype=np.float32)
    cache: dict[str, np.ndarray] = {}
    for i, b in enumerate(basins):
        area = float(areas[b])
        outer = [g for g in up.get(b, []) if g["outermost"]]
        for g in outer:
            if g["site"] not in cache:
                cache[g["site"]] = discharge_series(root, g["site"], index)[0]
            q = cache[g["site"]]
            ok = np.isfinite(q)
            q_sum[i] += np.where(ok, q * 3.6 / area, 0.0).astype(np.float32)
            frac[i] += np.where(ok, min(g["area_km2"] / area, 1.0), 0.0).astype(np.float32)
        for k, g in enumerate(sorted(outer, key=lambda g: -g["area_km2"])[:N_UPSTREAM_SLOTS]):
            slot_q[i, k] = cache[g["site"]] * 3.6 / area
    q_sum[frac == 0] = np.nan
    return {"upstream_gauged_q_mm_h": q_sum, "upstream_gauged_frac": np.minimum(frac, 1.0), "upstream_slot_q_mm_h": slot_q}


def upstream_statics(basins: list[str], areas: pd.Series, up: dict) -> dict[str, np.ndarray]:
    k = N_UPSTREAM_SLOTS
    out = {
        "upstream_n_gauges": np.zeros(len(basins), np.float32),
        "upstream_n_gauges_all": np.zeros(len(basins), np.float32),
        "upstream_gauged_area_frac": np.zeros(len(basins), np.float32),
        "upstream_slot_area_frac": np.full((len(basins), k), np.nan, np.float32),
        "upstream_slot_distance_km": np.full((len(basins), k), np.nan, np.float32),
        "upstream_slot_travel_time_h": np.full((len(basins), k), np.nan, np.float32),
    }
    slot_sites = np.full((len(basins), k), "", dtype="<U15")
    sum_sites = []
    for i, b in enumerate(basins):
        gs = up.get(b, [])
        outer = sorted((g for g in gs if g["outermost"]), key=lambda g: -g["area_km2"])
        area = float(areas[b])
        out["upstream_n_gauges"][i] = len(outer)
        out["upstream_n_gauges_all"][i] = len(gs)
        out["upstream_gauged_area_frac"][i] = min(sum(g["area_km2"] for g in outer) / area, 1.0)
        for j, g in enumerate(outer[:k]):
            out["upstream_slot_area_frac"][i, j] = min(g["area_km2"] / area, 1.0)
            out["upstream_slot_distance_km"][i, j] = g["distance_km"]
            out["upstream_slot_travel_time_h"][i, j] = g["travel_time_h"]
            slot_sites[i, j] = g["site"]
        sum_sites.append(",".join(g["site"] for g in outer))
    out["upstream_slot_site"] = slot_sites
    out["upstream_sum_sites"] = np.array(sum_sites, dtype=f"<U{max(8, max(map(len, sum_sites), default=8))}")
    return out


UPSTREAM_ATTRS = {
    "upstream_gauged_q_mm_h": {"units": "mm/h", "long_name": "summed flow of outermost upstream gauges with data, over the target basin area"},
    "upstream_gauged_frac": {"units": "1", "long_name": "share of the target basin's area drained by upstream gauges with data at this hour"},
    "upstream_slot_q_mm_h": {"units": "mm/h", "long_name": "flow of the 3 largest outermost upstream gauges (slot 0 = largest), over the target basin area"},
    "upstream_n_gauges": {"long_name": "outermost (non-nested) upstream gauges"},
    "upstream_n_gauges_all": {"long_name": "all qualifying upstream gauges, nested included"},
    "upstream_gauged_area_frac": {"units": "1", "long_name": "share of the target area drained by the outermost upstream gauges"},
    "upstream_slot_area_frac": {"units": "1"},
    "upstream_slot_distance_km": {"units": "km", "long_name": "along-channel distance to the target (NHDPlusV2)"},
    "upstream_slot_travel_time_h": {"units": "h", "long_name": "mean-annual travel time to the target (NHDPlusV2 EROM; waterbodies at 1 m/s)"},
}


def add_v12(root: Path, subset: str) -> str:
    """v1.2 = the v1.1 stores plus upstream-gauge features. v1.1 arrays are untouched."""
    sel = pd.read_parquet(root / "selection.parquet")
    basins = json.loads((root / "slice.json").read_text()) if subset == "slice50" else list(sel.index)
    areas = sel["DRAIN_SQKM"].astype(float)
    up = json.loads((root / "upstream.json").read_text())
    src, dst = f"s3://{config.BUCKET}/v1.1/{subset}/", f"s3://{config.BUCKET}/{V12_PREFIX}/{subset}/"
    subprocess.run(["aws", "s3", "sync", "--only-show-errors", "--delete", src, dst], check=True)
    base_id = subprocess.run(["aws", "s3", "cp", src + "MANIFEST_ID", "-"], capture_output=True, text=True, check=True).stdout.strip()
    train = next(s for s in config.SPLITS if s.name == "train")
    statics = upstream_statics(basins, areas, up)
    manifest = {
        "version": "v1.2",
        "base": {"version": "v1.1", "manifest_id": base_id},
        "subset": subset,
        "created": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_sha": git_sha(),
        "basins": basins,
        "upstream": {b: up.get(b, []) for b in basins},
        "stores": {},
    }
    for store, (start, end) in config.STORES.items():
        index = config.hourly_index(start, end)
        train_mask = np.asarray((index >= train.start) & (index <= train.end))
        group = zarr.open_group(f"{dst}{store}.zarr", mode="r+", use_consolidated=False, storage_options={"anon": False})
        dyn = upstream_arrays(root, basins, areas, up, index)
        stats = {}
        _create(group, "upstream_slot", np.arange(N_UPSTREAM_SLOTS, dtype=np.int8), ("upstream_slot",), {"description": "0 = largest outermost upstream gauge"}, basin_axis=False)
        for name, data in dyn.items():
            dims = ("basin", "upstream_slot", "time") if data.ndim == 3 else ("basin", "time")
            rs = RunningStats(train_mask, data.ndim - 1)
            rs.add(data)
            stats[name] = rs.result()
            _create(group, name, data, dims, UPSTREAM_ATTRS[name] | stats[name])
        for name, data in statics.items():
            dims = ("basin", "upstream_slot") if data.ndim == 2 else ("basin",)
            _create(group, name, data, dims, UPSTREAM_ATTRS.get(name, {}), basin_axis=False)
        group.attrs.update({"version": "v1.2", "v1_2_additions": sorted([*dyn, *statics])})
        zarr.consolidate_metadata(group.store)
        digest, nbytes = listing_hash(config.BUCKET, f"{V12_PREFIX}/{subset}/{store}.zarr/")
        manifest["stores"][store] = {"listing_sha256": digest, "bytes": nbytes, "stats": stats}
        log.info("v1.2 %s %s: upstream arrays added, %.2f GB total", subset, store, nbytes / 1e9)
    text = json.dumps(manifest, indent=1, default=str)
    manifest_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    s3.put_object(Bucket=config.BUCKET, Key=f"{V12_PREFIX}/{subset}/manifest.json", Body=text.encode())
    s3.put_object(Bucket=config.BUCKET, Key=f"{V12_PREFIX}/{subset}/MANIFEST_ID", Body=(manifest_id + "\n").encode())
    log.info("published %s (manifest %s)", dst, manifest_id)
    return manifest_id


V13_PREFIX = os.environ.get("FLOWCAST_V13_PREFIX", "v1.3")
N_TZ3, N_TZ1H = 3, 73  # traveltime.N_COARSE, traveltime.N_HOURLY


def zone_fractions(root: Path, sel: list[str], basins: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """(basins, 3) and (basins, 73) area fractions of each travel-time zone (the basin's width function)."""
    out = []
    for name, n in (("weights_aorc_tz3.npz", N_TZ3), ("weights_aorc_tz1h.npz", N_TZ1H)):
        w = np.asarray(sp.load_npz(root / name).sum(axis=1)).ravel().reshape(len(sel), n)
        w = w[[sel.index(b) for b in basins]]
        out.append((w / w.sum(axis=1, keepdims=True)).astype(np.float32))
    return out[0], out[1]


def add_v13(root: Path, run: str, subset: str) -> str:
    """v1.3 = the v1.2 stores plus travel-time zone forcings. v1.2 arrays are untouched.

    Precipitation per zone is stored as area fraction x zone mean (mm/h over the whole basin), so zones sum to the
    basin mean; temperature per zone is the zone mean. Zones with no area are NaN.
    """
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    basins = json.loads((root / "slice.json").read_text()) if subset == "slice50" else sel
    download_extract(root, run, "all")
    az = ExtractReader(root, run, "aorc_zones", "all", keep_compressed=False)
    gz = ExtractReader(root, run, "gefs_forecast_zones", "all", keep_compressed=False)
    rz = ExtractReader(root, run, "gefs_reforecast", "all", keep_compressed=False)
    src, dst = f"s3://{config.BUCKET}/v1.2/{subset}/", f"s3://{config.BUCKET}/{V13_PREFIX}/{subset}/"
    subprocess.run(["aws", "s3", "sync", "--only-show-errors", "--delete", src, dst], check=True)
    base_id = subprocess.run(["aws", "s3", "cp", src + "MANIFEST_ID", "-"], capture_output=True, text=True, check=True).stdout.strip()
    f3, f1 = zone_fractions(root, sel, basins)
    summary = pd.read_parquet(root / "traveltime_summary.parquet").loc[basins]
    no_network = summary["n_flowlines"].to_numpy() == 0  # gauge not indexed to NHDPlus: zones unknown, not "all near the outlet"
    f3[no_network], f1[no_network] = np.nan, np.nan
    summary.loc[no_network, ["tt_mean_h", "tt_p90_h", "tt_max_h"]] = np.nan
    train = next(s for s in config.SPLITS if s.name == "train")
    manifest = {
        "version": "v1.3", "base": {"version": "v1.2", "manifest_id": base_id}, "subset": subset, "run": run,
        "created": pd.Timestamp.now(tz="UTC").isoformat(), "git_sha": git_sha(), "basins": basins, "stores": {},
    }
    mask3 = np.where(f3 > 0, 1.0, np.nan).astype(np.float32)
    mask1 = np.where(f1 > 0, 1.0, np.nan).astype(np.float32)
    blocks = [(i, min(i + SHARD_BASINS, len(basins))) for i in range(0, len(basins), SHARD_BASINS)]
    for store, (start, end) in config.STORES.items():
        index = config.hourly_index(start, end)
        train_mask = np.asarray((index >= train.start) & (index <= train.end))
        group = zarr.open_group(f"{dst}{store}.zarr", mode="r+", use_consolidated=False, storage_options={"anon": False})
        _create(group, "tz_coarse", np.arange(N_TZ3, dtype=np.int8), ("tz_coarse",), {"description": "travel-time zones: 0 = 0-6 h, 1 = 6-24 h, 2 = 24 h+"}, basin_axis=False)
        _create(group, "tz_hourly", np.arange(N_TZ1H, dtype=np.int16), ("tz_hourly",), {"description": "1 h travel-time bins; k = [k, k+1) h, 72 = 72 h+"}, basin_axis=False)
        prec_attrs = {"units": "mm/h", "description": "zone area fraction x zone-mean precipitation (zones sum to the basin mean); NaN for zones with no area"}
        arrays = {
            "aorc_tz1h_precip_mm_h": (_empty(group, "aorc_tz1h_precip_mm_h", (len(basins), N_TZ1H, len(index)), np.float32, ("basin", "tz_hourly", "time"), prec_attrs | {"source": "aorc"}), RunningStats(train_mask, 2)),
            "aorc_tz3_precip_mm_h": (_empty(group, "aorc_tz3_precip_mm_h", (len(basins), N_TZ3, len(index)), np.float32, ("basin", "tz_coarse", "time"), prec_attrs | {"source": "aorc"}), RunningStats(train_mask, 2)),
            "aorc_tz3_temp_2m_c": (_empty(group, "aorc_tz3_temp_2m_c", (len(basins), N_TZ3, len(index)), np.float32, ("basin", "tz_coarse", "time"), {"units": "degC", "source": "aorc", "description": "zone-mean temperature"}), RunningStats(train_mask, 2)),
        }
        for i0, i1 in blocks:
            blk = basins[i0:i1]
            d3 = az.series([f"{b}/tz3_{k}" for b in blk for k in range(N_TZ3)], index).reshape(len(blk), N_TZ3, len(index), 2)
            d1 = az.series([f"{b}/tz1h_{k}" for b in blk for k in range(N_TZ1H)], index).reshape(len(blk), N_TZ1H, len(index), 2)
            for name, data in (
                ("aorc_tz1h_precip_mm_h", d1[..., 0] * (f1[i0:i1] * mask1[i0:i1])[..., None]),
                ("aorc_tz3_precip_mm_h", d3[..., 0] * (f3[i0:i1] * mask3[i0:i1])[..., None]),
                ("aorc_tz3_temp_2m_c", d3[..., 1] * mask3[i0:i1][..., None]),
            ):
                arrays[name][0][i0:i1] = data
                arrays[name][1].add(data)
            log.info("v1.3 %s %s: aorc zones basins %d-%d", subset, store, i0, i1)
        stats = {}
        for name, (arr, rs) in arrays.items():
            stats[name] = rs.result()
            arr.attrs.update(stats[name])

        forecast_sets = [("gefs_tz3_precip_mm_h", gz, "gefs", pd.to_datetime(group["gefs_init"][:], unit="h", origin=HOURS_EPOCH).tz_localize("UTC"))]
        rf_inits = rz.inits(start, end)
        if len(rf_inits):
            forecast_sets.append(("gefs_rf_tz3_precip_mm_h", rz, "gefs_rf", rf_inits))
        for name, reader, dim, inits in forecast_sets:
            shape = reader.shards[0][1].shape
            members, leads = shape[2], shape[3]
            outputs = json.loads(next(iter(reader.dir.glob("*/all.json"))).read_text())["variables"]
            arr = _empty(group, name, (len(basins), len(inits), members, leads, N_TZ3), np.float32, ("basin", f"{dim}_init", f"{dim}_member", f"{dim}_lead", "tz_coarse"), prec_attrs | {"source": reader.source})
            init_train = np.asarray((inits >= train.start) & (inits <= train.end))
            rs = RunningStats(init_train, 1)
            k_p = outputs.index("precip_mm_h")
            for i0, i1 in blocks:
                blk = basins[i0:i1]
                d = reader.forecasts([f"{b}/tz3_{k}" for b in blk for k in range(N_TZ3)], inits)[..., k_p]
                d = d.reshape(len(blk), N_TZ3, *d.shape[1:])
                qc_range(d, "precip_mm_h")
                d = np.moveaxis(d, 1, -1) * (f3[i0:i1] * mask3[i0:i1])[:, None, None, None, :]
                arr[i0:i1] = d
                rs.add(d)
            stats[name] = rs.result()
            arr.attrs.update(stats[name])
            log.info("v1.3 %s %s: %s written", subset, store, name)

        _create(group, "tz3_area_frac", f3, ("basin", "tz_coarse"), {"units": "1", "description": "area fraction per coarse travel-time zone"}, basin_axis=False)
        _create(group, "tz1h_area_frac", f1, ("basin", "tz_hourly"), {"units": "1", "description": "area fraction per 1 h travel-time bin (width function)"}, basin_axis=False)
        for col in ("tt_mean_h", "tt_p90_h", "tt_max_h"):
            _create(group, col, summary[col].to_numpy(np.float32), ("basin",), {"units": "h", "description": "travel time to the outlet over basin cells (NHDPlus channel + hillslope)"}, basin_axis=False)
        group.attrs.update({"version": "v1.3", "v1_3_additions": sorted([*stats, "tz3_area_frac", "tz1h_area_frac", "tt_mean_h", "tt_p90_h", "tt_max_h"])})
        zarr.consolidate_metadata(group.store)
        digest, nbytes = listing_hash(config.BUCKET, f"{V13_PREFIX}/{subset}/{store}.zarr/")
        manifest["stores"][store] = {"listing_sha256": digest, "bytes": nbytes, "stats": stats}
        log.info("v1.3 %s %s done, %.2f GB", subset, store, nbytes / 1e9)
    text = json.dumps(manifest, indent=1, default=str)
    manifest_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    s3.put_object(Bucket=config.BUCKET, Key=f"{V13_PREFIX}/{subset}/manifest.json", Body=text.encode())
    s3.put_object(Bucket=config.BUCKET, Key=f"{V13_PREFIX}/{subset}/MANIFEST_ID", Body=(manifest_id + "\n").encode())
    log.info("published %s (manifest %s)", dst, manifest_id)
    return manifest_id


def assemble_cube(root: Path, run: str, subset: str, upload: bool) -> None:
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    early = json.loads((root / "slice.json").read_text())
    basins = early if subset == "slice50" else sel
    kind = "slice" if subset == "slice50" else "all"
    download_extract(root, run, kind)
    readers = {s: ExtractReader(root, run, s, kind) for s in ("aorc", "hrrr_analysis", "mrms", "hrrr_forecast", "gefs_forecast")}
    readers = {s: r for s, r in readers.items() if r.shards}
    missing = sorted(set(SOURCES) - set(readers))
    out = root / "cube" / config.VERSION / subset
    manifest = {
        "version": config.VERSION,
        "subset": subset,
        "run": run,
        "created": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_sha": git_sha(),
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
