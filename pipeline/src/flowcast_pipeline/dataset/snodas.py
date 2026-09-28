"""SNODAS daily snowpack per basin and elevation band, appended to the v1.3 stores in place.

SNODAS (NOHRSC, 1 km, daily at 06 UTC) assimilates station, airborne and satellite snow observations into a snow
model, so unlike the free-running SNOW-17 module it carries a measured snowpack. Sources:

* USGS HyTEST's SNODAS Zarr on OSN (2006-01-01 to 2025-07-01, the masked CONUS grid), and
* NSIDC G02158 daily masked tarballs for the rest (2003-09-30 to 2005-12-31 and 2025-07-02 on).

Both are the same 6935 x 3351 grid. It is offset from the AORC grid by half a cell, so the AORC basin and band weights
(`weights_aorc_all.npz`) are remapped by area overlap, not rebuilt. Pre-Oct-2013 NSIDC headers put the grid 0.0004
degrees (40 m) away from the later ones; the array layout is identical and the offset is ignored.

Available at issue time: the product valid at 06 UTC on day D is published around 13 UTC that day. The hourly arrays
use it from 00 UTC on D+1 through 23 UTC on D+1 (18-41 h old), i.e. "the value from the day before". After a
missing day the latest product is carried forward for up to `MAX_AGE_DAYS`; `snodas_age_h` is the age used.
"""

import gzip
import hashlib
import json
import logging
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import requests
import scipy.sparse as sp
import zarr
from zarr.storage import FsspecStore

from . import config
from .cube import N_BANDS, SHARD_BASINS, V13_PREFIX, RunningStats, _create, _empty, git_sha, listing_hash
from .extract import MIN_VALID_WEIGHT
from .grids import AORC, GEOGRAPHIC, Grid

log = logging.getLogger(__name__)

# NSIDC files are north-up; the HyTEST Zarr stores the same rows south-up, chunked 6 days x 445 x 919.
GRID = Grid("snodas", GEOGRAPHIC.to_wkt(), -124.729166666667, 1 / 120, 6935, 52.870833333333, -1 / 120, 3351, 445, 919)
HYTEST_GRID = Grid("snodas_hytest", GEOGRAPHIC.to_wkt(), GRID.x0, GRID.dx, GRID.nx, GRID.y0 + GRID.dy * (GRID.ny - 1), -GRID.dy, GRID.ny, 445, 919)
FIRST_DAY = pd.Timestamp("2003-09-30")
HYTEST_STORE = "s3://hytest/snodas.zarr"
OSN_ENDPOINT = "https://usgs.osn.mghpcc.org"
HYTEST_DAYS = (pd.Timestamp("2006-01-01"), pd.Timestamp("2025-07-01"))
NSIDC_URL = "https://noaadata.apps.nsidc.org/NOAA/G02158/masked/{d:%Y}/{d:%m_%b}/SNODAS_{d:%Y%m%d}.tar"
VALID_HOUR_UTC = 6
MAX_AGE_DAYS = 7

# variable: (HyTEST name, NSIDC product code, NSIDC integer -> output unit, HyTEST metres -> output unit)
VARIABLES = {
    "swe": ("SWE", "1034", 1.0, 1000.0),  # mm
    "depth": ("SDP", "1036", 1.0, 1000.0),  # mm
    "melt": ("SNM", "1044", 0.01 / 24, 1000.0 / 24),  # 24 h snowmelt runoff ending at the valid time, as mm/h
}

ARRAYS = {
    "snodas_swe_mm": ("swe", False, {"units": "mm", "description": "SNODAS snow water equivalent, basin mean"}),
    "snodas_depth_mm": ("depth", False, {"units": "mm", "description": "SNODAS snow depth, basin mean"}),
    "snodas_melt_mm_h": ("melt", False, {"units": "mm/h", "description": "SNODAS snowmelt runoff over the 24 h ending at the valid time, as a mean rate, basin mean"}),
    "snodas_band_swe_mm": ("swe", True, {"units": "mm", "description": "SNODAS snow water equivalent per elevation band (the AORC bands)"}),
    "snodas_band_melt_mm_h": ("melt", True, {"units": "mm/h", "description": "SNODAS snowmelt runoff per elevation band, 24 h ending at the valid time, as a mean rate"}),
}
TIMING = (
    f"value at t is the SNODAS product valid {VALID_HOUR_UTC:02d} UTC on the UTC day before t (published the same day), "
    f"carried forward up to {MAX_AGE_DAYS} more days over missing days; NaN before 2003-10-01 and where under "
    f"{MIN_VALID_WEIGHT:.0%} of the area has SNODAS cells"
)


# ------------------------------------------------------------------------------------------ weights


def _axis_overlap(centres: np.ndarray, src: Grid, axis: str) -> tuple[np.ndarray, np.ndarray]:
    """For same-resolution grids: the two `src` indices overlapping each cell centre and the first one's share."""
    c0, d = (src.x0, src.dx) if axis == "x" else (src.y0, src.dy)
    pos = (centres - c0) / d
    j0 = np.floor(pos).astype(np.int64)
    return j0, 1.0 - (pos - j0)


def remap_weights(w: sp.csr_matrix, src: Grid = AORC, dst: Grid = GRID) -> sp.csr_matrix:
    """Move unit weights on `src` cells onto `dst` cells by area overlap (both grids at the same resolution)."""
    if not (np.isclose(abs(src.dx), abs(dst.dx)) and np.isclose(abs(src.dy), abs(dst.dy))):
        raise ValueError("remap_weights needs grids of the same resolution")
    coo = w.tocoo()
    iy, ix = np.divmod(coo.col, src.nx)
    jx, fx = _axis_overlap(src.x0 + src.dx * ix, dst, "x")
    jy, fy = _axis_overlap(src.y0 + src.dy * iy, dst, "y")
    rows, cols, vals = [], [], []
    for oy, sy in ((0, fy), (1, 1.0 - fy)):
        for ox, sx in ((0, fx), (1, 1.0 - fx)):
            yy, xx = jy + oy, jx + ox
            ok = (yy >= 0) & (yy < dst.ny) & (xx >= 0) & (xx < dst.nx) & (sy * sx > 1e-9)
            rows.append(coo.row[ok])
            cols.append(yy[ok] * dst.nx + xx[ok])
            vals.append((coo.data * sy * sx)[ok])
    out = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(w.shape[0], dst.ny * dst.nx))
    out.sum_duplicates()
    return out


class Reducer:
    """Weighted unit means of SNODAS fields, per source chunk (tile), with the shared valid-area threshold."""

    def __init__(self, w: sp.csr_matrix, grid: Grid = GRID):
        self.grid = grid
        self.wsum = np.asarray(w.sum(axis=1)).ravel()
        csc = w.tocsc()
        cells = np.unique(w.indices)
        iy, ix = np.divmod(cells, grid.nx)
        tiles = grid.tile_of(iy, ix)
        self.tiles: dict[int, tuple[slice, slice, np.ndarray, sp.csr_matrix]] = {}
        for t in np.unique(tiles):
            sel = tiles == t
            ty, tx = divmod(int(t), grid.tiles_x)
            ys = slice(ty * grid.chunk_y, min((ty + 1) * grid.chunk_y, grid.ny))
            xs = slice(tx * grid.chunk_x, min((tx + 1) * grid.chunk_x, grid.nx))
            local = (iy[sel] - ys.start) * (xs.stop - xs.start) + (ix[sel] - xs.start)
            self.tiles[int(t)] = (ys, xs, local, csc[:, cells[sel]].T.tocsr())

    def partial(self, tile: int, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(weighted sum, valid weight), each (units, days), for fields x (days, tile rows, tile cols)."""
        _, _, local, wt = self.tiles[tile]
        v = x.reshape(x.shape[0], -1)[:, local].astype(np.float64)
        ok = np.isfinite(v) & (v >= 0)
        return (wt.T @ np.where(ok, v, 0.0).T), (wt.T @ ok.T.astype(np.float64))

    def finish(self, num: np.ndarray, den: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den >= MIN_VALID_WEIGHT * self.wsum[:, None], num / den, np.nan).astype(np.float32)


# ------------------------------------------------------------------------------------------ sources


def _hytest_group() -> zarr.Group:
    store = FsspecStore.from_url(HYTEST_STORE, storage_options={"anon": True, "client_kwargs": {"endpoint_url": OSN_ENDPOINT}}, read_only=True)
    return zarr.open_group(store, mode="r")


def land_fields(fields: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """SNODAS leaves melt as no-data where there is no snowpack; on land (valid SWE) that is 0 melt."""
    melt = np.where(np.isfinite(fields["swe"]), np.nan_to_num(fields["melt"], nan=0.0), np.nan).astype(np.float32)
    return fields | {"melt": melt}


def reduce_fields(reducer: Reducer, fields: dict[str, np.ndarray]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-variable (weighted sum, valid weight) of full-grid fields (days, ny, nx)."""
    out = {}
    for var, x in land_fields(fields).items():
        num = den = 0.0
        for tile, (ys, xs, _, _) in reducer.tiles.items():
            n, d = reducer.partial(tile, x[:, ys, xs])
            num, den = num + n, den + d
        out[var] = (num, den)
    return out


def hytest_block(group: zarr.Group, reducer: Reducer, t0: int, t1: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Days t0:t1 of the HyTEST store, read tile by tile (only the source chunks that hold basin cells)."""
    out: dict[str, tuple] = {}
    for tile, (ys, xs, _, _) in reducer.tiles.items():
        fields = {var: group[name][t0:t1, ys, xs] * np.float32(scale) for var, (name, _, _, scale) in VARIABLES.items()}
        for var, x in land_fields(fields).items():
            n, d = reducer.partial(tile, x)
            num, den = out.get(var, (0.0, 0.0))
            out[var] = (num + n, den + d)
    return out


def nsidc_day(day: pd.Timestamp, cache: Path) -> dict[str, np.ndarray] | None:
    """The three fields of one NSIDC daily tarball in output units, or None if that day is missing upstream."""
    path = cache / f"SNODAS_{day:%Y%m%d}.tar"
    if not path.exists():
        for attempt in range(6):
            try:
                r = requests.get(NSIDC_URL.format(d=day), timeout=300)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                break
            except requests.RequestException:
                # NSIDC's server returns occasional 5xx under parallel load
                if attempt == 5:
                    raise
                time.sleep(4 * 2**attempt)
        path.write_bytes(r.content)
    fields = {}
    with tarfile.open(path) as tar:
        for member in tar.getmembers():
            for var, (_, code, scale, _) in VARIABLES.items():
                if f"ssmv1{code}" in member.name and member.name.endswith(".dat.gz"):
                    raw = np.frombuffer(gzip.decompress(tar.extractfile(member).read()), dtype=">i2").reshape(GRID.ny, GRID.nx)
                    fields[var] = np.where(raw == -9999, np.nan, raw.astype(np.float32) * np.float32(scale))
    return fields if len(fields) == len(VARIABLES) else None


def extract_daily(root: Path, end: pd.Timestamp, workers: int = 16) -> Path:
    """Daily SNODAS per AORC unit (basins, then basin bands) from FIRST_DAY to `end`, saved to root/snodas_daily.npz."""
    w_aorc = sp.load_npz(root / "weights_aorc_all.npz").tocsr()
    w = remap_weights(w_aorc, dst=HYTEST_GRID)
    reducer = Reducer(w, HYTEST_GRID)
    nsidc_reducer = Reducer(remap_weights(w_aorc, dst=GRID), GRID)
    days = pd.date_range(FIRST_DAY, end, freq="D")
    n_units = w.shape[0]
    daily = {var: np.full((n_units, len(days)), np.nan, dtype=np.float32) for var in VARIABLES}
    have = np.zeros(len(days), dtype=bool)
    valid_frac = np.full(n_units, np.nan, dtype=np.float32)

    hytest_cache = root / "snodas_hytest.npz"
    if hytest_cache.exists():
        with np.load(hytest_cache) as f:
            hdays = pd.DatetimeIndex(f["days"])
            pos = days.get_indexer(hdays)
            for var in VARIABLES:
                daily[var][:, pos] = f[var]
            valid_frac = f["valid_frac"]
    else:
        group = _hytest_group()
        unit, _, epoch = group["time"].attrs["units"].partition(" since ")  # "days since 2006-01-01 00:00:00"
        htimes = (pd.Timestamp(epoch) + pd.to_timedelta(group["time"][:], unit=unit)).normalize()
        chunk = group["SWE"].chunks[0]
        blocks = [(i, min(i + chunk, len(htimes))) for i in range(0, len(htimes), chunk)]

        def run_hytest(b):
            return b, hytest_block(group, reducer, *b)

        with ThreadPoolExecutor(workers) as ex:
            for k, ((t0, t1), res) in enumerate(ex.map(run_hytest, blocks)):
                pos = days.get_indexer(htimes[t0:t1])
                for var, (num, den) in res.items():
                    daily[var][:, pos] = reducer.finish(num, den)
                if k == 0:
                    valid_frac = (res["swe"][1].max(axis=1) / reducer.wsum).astype(np.float32)
                if k % 100 == 0:
                    log.info("snodas hytest %s (%d of %d blocks)", htimes[t0].date(), k, len(blocks))
        pos = days.get_indexer(htimes)
        np.savez(hytest_cache, days=htimes.to_numpy(), valid_frac=valid_frac, **{var: daily[var][:, pos] for var in VARIABLES})
    have[:] = np.isfinite(daily["swe"]).any(axis=0)

    cache = root / "snodas_nsidc"
    cache.mkdir(exist_ok=True)
    rest = [d for d in days if not (HYTEST_DAYS[0] <= d <= HYTEST_DAYS[1])]

    def run_nsidc(day):
        fields = nsidc_day(day, cache)
        if fields is None:
            return day, None
        sums = reduce_fields(nsidc_reducer, {var: x[None] for var, x in fields.items()})
        return day, {var: nsidc_reducer.finish(*nd)[:, 0] for var, nd in sums.items()}

    with ThreadPoolExecutor(min(workers, 8)) as ex:
        for k, (day, res) in enumerate(ex.map(run_nsidc, rest)):
            if res is None:
                log.warning("snodas nsidc %s missing upstream", day.date())
                continue
            i = days.get_loc(day)
            for var, v in res.items():
                daily[var][:, i] = v
            have[i] = np.isfinite(res["swe"]).any()
            if k % 100 == 0:
                log.info("snodas nsidc %s (%d of %d days)", day.date(), k, len(rest))

    out = root / "snodas_daily.npz"
    np.savez_compressed(out, days=days.to_numpy(), have=have, valid_frac=valid_frac, **daily)
    log.info("snodas daily: %d days, %d missing, saved %s", len(days), int((~have).sum()), out)
    boto3.client("s3", region_name=config.AWS_REGION).upload_file(str(out), config.BUCKET, "work/snodas/snodas_daily.npz")
    return out


# ------------------------------------------------------------------------------------------ hourly arrays


def hourly_positions(days: pd.DatetimeIndex, have: np.ndarray, index: pd.DatetimeIndex, now: pd.Timestamp) -> tuple[np.ndarray, np.ndarray]:
    """Per hour: the daily position of the latest product available at that hour (-1 if none), and its age in hours."""
    last = np.maximum.accumulate(np.where(have, np.arange(len(days)), -1))
    hours = index.tz_convert(None) if index.tz is not None else index
    latest = days.searchsorted(hours.floor("D") - pd.Timedelta(days=1), side="right") - 1
    src = np.where(latest >= 0, last[np.maximum(latest, 0)], -1)
    valid_at = days.to_numpy()[np.maximum(src, 0)] + np.timedelta64(VALID_HOUR_UTC, "h")
    age = ((hours.to_numpy() - valid_at) / np.timedelta64(1, "h")).astype(np.float32)
    now = now.tz_convert(None) if now.tz is not None else now
    stale = (src < 0) | (age > 24 * MAX_AGE_DAYS + 41) | (hours.to_numpy() > now.to_datetime64())
    return np.where(stale, -1, src), np.where(stale, np.nan, age).astype(np.float32)


def add_snodas(root: Path, subset: str, now: pd.Timestamp | None = None) -> str:
    """Append the SNODAS arrays to the v1.3 `subset` stores in place (existing arrays untouched)."""
    now = now or pd.Timestamp.now(tz="UTC")
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    basins = json.loads((root / "slice.json").read_text()) if subset == "slice50" else sel
    pos = np.array([sel.index(b) for b in basins])
    band_rows = (len(sel) + pos[:, None] * N_BANDS + np.arange(N_BANDS)[None, :]).ravel()
    with np.load(root / "snodas_daily.npz") as f:
        z = {k: f[k] for k in f.files}
    days, have = pd.DatetimeIndex(z["days"]), z["have"]
    dst = f"s3://{config.BUCKET}/{V13_PREFIX}/{subset}/"
    prev_manifest = json.loads(subprocess.run(["aws", "s3", "cp", dst + "manifest.json", "-"], capture_output=True, text=True, check=True).stdout)
    prev_id = subprocess.run(["aws", "s3", "cp", dst + "MANIFEST_ID", "-"], capture_output=True, text=True, check=True).stdout.strip()
    train = next(s for s in config.SPLITS if s.name == "train")
    blocks = [(i, min(i + SHARD_BASINS, len(basins))) for i in range(0, len(basins), SHARD_BASINS)]
    train_stats: dict[str, dict] = {}
    added: dict[str, dict] = {}
    for store, (start, end) in config.STORES.items():
        index = config.hourly_index(start, end)
        train_mask = np.asarray((index >= train.start) & (index <= train.end))
        src, age = hourly_positions(days, have, index, now)
        ok = src >= 0
        group = zarr.open_group(f"{dst}{store}.zarr", mode="r+", use_consolidated=False, storage_options={"anon": False})
        stored = list(group["basin"][:])
        if stored != basins:
            raise RuntimeError(f"{subset} {store}: basin axis differs from the local selection")
        arrays = {}
        for name, (var, banded, attrs) in ARRAYS.items():
            shape = (len(basins), N_BANDS, len(index)) if banded else (len(basins), len(index))
            dims = ("basin", "band", "time") if banded else ("basin", "time")
            meta = attrs | {"source": "SNODAS (NOHRSC; HyTEST Zarr and NSIDC G02158)", "timing": TIMING}
            arrays[name] = (_empty(group, name, shape, np.float32, dims, meta), RunningStats(train_mask, len(shape) - 1), var, banded)
        for i0, i1 in blocks:
            for name, (arr, rs, var, banded) in arrays.items():
                rows = band_rows[i0 * N_BANDS : i1 * N_BANDS] if banded else pos[i0:i1]
                d = z[var][rows]
                x = np.where(ok[None, :], d[:, np.maximum(src, 0)], np.nan).astype(np.float32)
                x = x.reshape(i1 - i0, N_BANDS, len(index)) if banded else x
                arr[i0:i1] = x
                rs.add(x)
            log.info("snodas %s %s: basins %d-%d", subset, store, i0, i1)
        age_attrs = {"units": "h", "description": "age of the SNODAS product used at t (hours since its 06 UTC valid time)", "timing": TIMING}
        _create(group, "snodas_age_h", np.where(np.isfinite(z["swe"][pos][:, np.maximum(src, 0)]) & ok[None, :], age[None, :], np.nan).astype(np.float32), ("basin", "time"), age_attrs)
        _create(group, "snodas_valid_frac", z["valid_frac"][pos].astype(np.float32), ("basin",), {"units": "1", "description": "share of the basin's area weight on cells inside the SNODAS (CONUS) mask"}, basin_axis=False)
        stats = {}
        for name, (arr, rs, _, _) in arrays.items():
            # Normalization comes from the train split, so the test store carries the trainval statistics.
            stats[name] = train_stats.setdefault(name, rs.result())
            arr.attrs.update(stats[name])
        group.attrs.update({"v1_3_snodas_additions": sorted([*ARRAYS, "snodas_age_h", "snodas_valid_frac"])})
        zarr.consolidate_metadata(group.store)
        added[store] = stats
        digest, nbytes = listing_hash(config.BUCKET, f"{V13_PREFIX}/{subset}/{store}.zarr/")
        prev_manifest["stores"][store].update({"listing_sha256": digest, "bytes": nbytes})
        prev_manifest["stores"][store].setdefault("stats", {}).update(stats)
    prev_manifest["snodas_addition"] = {
        "created": pd.Timestamp.now(tz="UTC").isoformat(), "git_sha": git_sha(), "previous_manifest_id": prev_id,
        "arrays": sorted([*ARRAYS, "snodas_age_h", "snodas_valid_frac"]), "timing": TIMING,
        "days": [str(days[0].date()), str(days[-1].date())], "missing_days": [str(d.date()) for d in days[~have]],
        "daily_sha256": hashlib.sha256((root / "snodas_daily.npz").read_bytes()).hexdigest(),
    }
    text = json.dumps(prev_manifest, indent=1, default=str)
    manifest_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    s3.put_object(Bucket=config.BUCKET, Key=f"{V13_PREFIX}/{subset}/manifest.json", Body=text.encode())
    s3.put_object(Bucket=config.BUCKET, Key=f"{V13_PREFIX}/{subset}/MANIFEST_ID", Body=(manifest_id + "\n").encode())
    log.info("updated %s (manifest %s, was %s)", dst, manifest_id, prev_id)
    return manifest_id


def export_store(subset: str, store: str, dest: str) -> None:
    """Copy the SNODAS arrays (plus basin/time/band coordinates) of a v1.3 store into a standalone store at `dest`.

    For training runs pinned to an older copy of the cube: they read that copy plus this store, so every other input
    stays exactly as it was.
    """
    src = f"s3://{config.BUCKET}/{V13_PREFIX}/{subset}/{store}.zarr/"
    names = ["basin", "time", "band", *ARRAYS, "snodas_age_h", "snodas_valid_frac"]
    include = [arg for n in names for arg in ("--include", f"{n}/*")]
    subprocess.run(["aws", "s3", "sync", "--only-show-errors", src, dest.rstrip("/") + "/", "--exclude", "*", *include], check=True)
    group = zarr.open_group(dest, mode="a", storage_options={"anon": False})
    group.attrs.update({"source": src, "arrays": names[3:]})
    zarr.consolidate_metadata(group.store)
    log.info("exported %s SNODAS arrays to %s", src, dest)
