"""National Water Model baselines for one reach.

* v3.0 retrospective (AORC-forced, no data assimilation, Feb 1979 - Jan 2023) from the public Zarr store.
  It is a simulation, not a forecast: score it as `run_type="simulation"`.
* Archived v3.0 operational forecasts from `noaa-nwm-pds` (Jan 2025+), read with HTTP range requests so
  only the streamflow array of each CONUS file is transferred (~3-4 MB per file).

There is no public NWM reforecast; archived operational forecasts are the closest substitute.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fsspec
import h5py
import numcodecs
import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

CFS_PER_CMS = 35.314666721
RETRO_ZARR = "https://noaa-nwm-retrospective-3-0-pds.s3.amazonaws.com/CONUS/zarr/chrtout.zarr"
RETRO_T0 = pd.Timestamp("1979-02-01T01:00", tz="UTC")
RETRO_MISSING = -999900
OPS_BUCKET = "https://noaa-nwm-pds.s3.amazonaws.com"
PRODUCTS = {
    # product: (directory, file stem, max lead h, lead step h)
    "short_range": ("short_range", "short_range.channel_rt", 18, 1),
    "medium_range_mem1": ("medium_range_mem1", "medium_range.channel_rt_1", 240, 1),
    **{f"medium_range_mem{k}": (f"medium_range_mem{k}", f"medium_range.channel_rt_{k}", 204, 3) for k in range(2, 7)},
    "medium_range_blend": ("medium_range_blend", "medium_range_blend.channel_rt", 240, 1),
}


def _cache_root() -> Path:
    env = os.environ.get("FLOWCAST_CACHE_DIR")
    return (Path(env) if env else Path.home() / ".cache" / "flowcast") / "nwm"


# ------------------------------------------------------------------ retrospective


def retrospective(reach: int, start: str = "1979-02-01", end: str = "2023-02-01", workers: int = 8) -> pd.Series:
    """Hourly v3.0 retrospective streamflow (ft3/s, UTC index) for one reach."""
    cache = _cache_root() / "retrospective" / str(reach)
    cache.mkdir(parents=True, exist_ok=True)
    meta = requests.get(f"{RETRO_ZARR}/streamflow/.zarray", timeout=60).json()
    n_time, _ = meta["shape"]
    t_chunk, f_chunk = meta["chunks"]
    fid_path = cache / "feature_index.json"
    if fid_path.exists():
        col = json.loads(fid_path.read_text())["index"]
    else:
        raw = requests.get(f"{RETRO_ZARR}/feature_id/0", timeout=120).content
        ids = np.frombuffer(numcodecs.Zstd().decode(raw), dtype="<i8")
        col = int(np.flatnonzero(ids == reach)[0])
        fid_path.write_text(json.dumps({"reach": reach, "index": col}))
    codec = numcodecs.Zstd()
    t_first = int((pd.Timestamp(start, tz="UTC") - RETRO_T0) / pd.Timedelta(hours=1))
    t_last = int((pd.Timestamp(end, tz="UTC") - RETRO_T0) / pd.Timedelta(hours=1))
    chunks = range(max(0, t_first) // t_chunk, min(n_time - 1, t_last) // t_chunk + 1)

    def load(c: int) -> np.ndarray:
        path = cache / f"{c}.npy"
        if path.exists():
            return np.load(path)
        raw = requests.get(f"{RETRO_ZARR}/streamflow/{c}.{col // f_chunk}", timeout=300).content
        block = np.frombuffer(codec.decode(raw), dtype="<i4").reshape(-1, f_chunk)
        values = block[:, col % f_chunk].astype(float)
        values[values == RETRO_MISSING] = np.nan
        np.save(path, values)
        return values

    with ThreadPoolExecutor(workers) as pool:
        blocks = list(pool.map(load, chunks))
    values = np.concatenate(blocks) * 0.01 * CFS_PER_CMS
    times = RETRO_T0 + pd.to_timedelta(np.arange(chunks.start * t_chunk, chunks.start * t_chunk + len(values)), unit="h")
    series = pd.Series(values, index=times, name="nwm_retrospective")
    return series[(series.index >= pd.Timestamp(start, tz="UTC")) & (series.index <= pd.Timestamp(end, tz="UTC"))]


# ---------------------------------------------------------- operational archive


def _ops_url(product: str, cycle: pd.Timestamp, lead_h: int) -> str:
    directory, stem, _, _ = PRODUCTS[product]
    return f"{OPS_BUCKET}/nwm.{cycle:%Y%m%d}/{directory}/nwm.t{cycle:%H}z.{stem}.f{lead_h:03d}.conus.nc"


def _read_reach(url: str, reach: int, index_hint: int | None) -> tuple[float, int]:
    fs = fsspec.filesystem("https")
    with fs.open(url, block_size=2**20, cache_type="bytes") as f, h5py.File(f, "r") as h:
        idx = index_hint
        if idx is None:
            idx = int(np.flatnonzero(h["feature_id"][:] == reach)[0])
        sf = h["streamflow"]
        raw = sf[idx]
        scale = float(np.atleast_1d(sf.attrs.get("scale_factor", 1.0))[0])
        fill = sf.attrs.get("_FillValue")
        if fill is not None and raw == np.atleast_1d(fill)[0]:
            return np.nan, idx
        return float(raw) * scale * CFS_PER_CMS, idx


def operational_forecasts(
    reach: int,
    site_id: str,
    product: str,
    cycles: pd.DatetimeIndex,
    leads_h,
    workers: int = 16,
) -> pd.DataFrame:
    """Long-format forecasts (see `schema`) for one reach from archived operational NWM output.

    Results are cached per cycle, so interrupted pulls resume. Missing files (outages) are skipped.
    """
    _, _, max_lead, step = PRODUCTS[product]
    leads = [int(h) for h in leads_h if h <= max_lead and int(h) % step == 0 and h >= step]
    cache = _cache_root() / "operational" / product / str(reach)
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "feature_index.json"
    index_hint = json.loads(index_path.read_text())["index"] if index_path.exists() else None
    if index_hint is None:
        probe = next(c for c in cycles)
        _, index_hint = _read_reach(_ops_url(product, probe, leads[0]), reach, None)
        index_path.write_text(json.dumps({"reach": reach, "index": index_hint}))

    def load_cycle(cycle: pd.Timestamp) -> dict[int, float]:
        path = cache / f"{cycle:%Y%m%d%H}.json"
        if path.exists():
            return {int(k): v for k, v in json.loads(path.read_text()).items()}
        out: dict[int, float] = {}
        for lead in leads:
            try:
                out[lead], _ = _read_reach(_ops_url(product, cycle, lead), reach, index_hint)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("NWM %s %s f%03d unreadable: %s", product, cycle, lead, exc)
        if out or cycle < pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2):
            path.write_text(json.dumps(out))
        return out

    rows = []
    with ThreadPoolExecutor(workers) as pool:
        for cycle, values in zip(cycles, pool.map(load_cycle, cycles)):
            for lead, value in values.items():
                rows.append((cycle, cycle + pd.Timedelta(hours=lead), float(lead), value))
    member = int(product.removeprefix("medium_range_mem")) if product.startswith("medium_range_mem") else None
    df = pd.DataFrame(rows, columns=["issue_time", "valid_time", "lead_h", "value"])
    df = df.assign(site_id=site_id, variable="discharge", model=f"nwm_{product}", unit="ft3/s", run_type="operational")
    if member is not None:
        df["member"] = member
    return df


def medium_range_ensemble(reach: int, site_id: str, cycles: pd.DatetimeIndex, leads_h, members=range(1, 7), workers: int = 16) -> pd.DataFrame:
    """Members 1-6 of the NWM medium-range ensemble as one `nwm_medium_range_ensemble` model."""
    frames = [operational_forecasts(reach, site_id, f"medium_range_mem{k}", cycles, leads_h, workers) for k in members]
    df = pd.concat(frames, ignore_index=True)
    common = df.groupby(["issue_time", "lead_h"])["member"].transform("nunique") == len(list(members))
    return df[common].assign(model="nwm_medium_range_ensemble")
