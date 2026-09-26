"""Basin-mean precipitation for the strong baselines: observed (reanalysis) and archived forecasts.

* Basin: the NLDI upstream basin polygon of the site; grid cells whose centers fall inside it are averaged.
* Observed: ERA5 hourly precipitation from the Open-Meteo archive (CC BY 4.0) at the 0.25 degree cell
  centers. Reanalysis is not available in real time, so any forecast that uses it for the *future* is a
  perfect-forcing run. For the *past* it stands in for MRMS/Stage IV, which are available within ~1-2 h.
* Forecast: the NOAA GEFS 35-day archive on dynamical.org (00Z inits from Oct 2020, 31 members, 3-hourly),
  ensemble mean. GEFS 00Z output is complete ~5-6 h after the cycle, so it is used from 06Z.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xarray as xr

from flowcast_pipeline.usgs.params import site_id

log = logging.getLogger(__name__)

NLDI_BASIN = "https://api.water.usgs.gov/nldi/linked-data/nwissite/{site}/basin"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
GEFS_ZARR = "https://data.dynamical.org/noaa/gefs/forecast-35-day/latest.zarr?email=flowcast@jaismith.github.io"
GEFS_START = pd.Timestamp("2020-10-01", tz="UTC")
GEFS_LATENCY_H = 6.0
GEFS_MAX_LEAD_H = 216
GRID = 0.25


def _cache_dir() -> Path:
    env = os.environ.get("FLOWCAST_CACHE_DIR")
    path = (Path(env) if env else Path.home() / ".cache" / "flowcast") / "precip"
    path.mkdir(parents=True, exist_ok=True)
    return path


def basin_polygon(site: str) -> np.ndarray:
    """Outer ring [N, 2] (lon, lat) of the site's NLDI basin."""
    path = _cache_dir() / f"basin_{site_id(site)}.json"
    if not path.exists():
        resp = requests.get(NLDI_BASIN.format(site=site_id(site)), params={"f": "json"}, timeout=120)
        resp.raise_for_status()
        path.write_text(json.dumps(resp.json()["features"][0]["geometry"]))
    geom = json.loads(path.read_text())
    rings = [geom["coordinates"][0]] if geom["type"] == "Polygon" else [p[0] for p in geom["coordinates"]]
    return np.asarray(max(rings, key=len), float)


def inside(poly: np.ndarray, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Ray-casting point-in-polygon for arrays of points."""
    x, y = np.asarray(lon, float), np.asarray(lat, float)
    result = np.zeros(x.shape, bool)
    xs, ys = poly[:, 0], poly[:, 1]
    for i in range(len(poly) - 1):
        x1, y1, x2, y2 = xs[i], ys[i], xs[i + 1], ys[i + 1]
        crosses = (y1 > y) != (y2 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
        result ^= crosses & (x < x_at)
    return result


def basin_cells(site: str) -> list[tuple[float, float]]:
    """(lat, lon) centers of the 0.25 degree cells inside the basin (the nearest cell if none is)."""
    poly = basin_polygon(site)
    lons = np.arange(np.floor(poly[:, 0].min() / GRID) * GRID, poly[:, 0].max() + GRID, GRID)
    lats = np.arange(np.floor(poly[:, 1].min() / GRID) * GRID, poly[:, 1].max() + GRID, GRID)
    glon, glat = np.meshgrid(lons, lats)
    mask = inside(poly, glon, glat)
    if not mask.any():
        centroid = poly.mean(axis=0)
        return [(round(centroid[1] / GRID) * GRID, round(centroid[0] / GRID) * GRID)]
    return [(float(la), float(lo)) for la, lo in zip(glat[mask], glon[mask])]


def era5_basin_precip(site: str, start: str = "2000-01-01", end: str | None = None) -> pd.Series:
    """Hourly basin-mean ERA5 precipitation (mm per hour ending at the index time, UTC)."""
    end = end or f"{pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=7):%Y-%m-%d}"
    path = _cache_dir() / f"era5_{site_id(site)}_{start}_{end}.parquet"
    if path.exists():
        return pd.read_parquet(path)["precip_mm"]
    cells = basin_cells(site)
    frames = []
    for chunk_start in pd.date_range(start, end, freq="5YS").union([pd.Timestamp(start)]):
        chunk_end = min(chunk_start + pd.DateOffset(years=5) - pd.Timedelta(days=1), pd.Timestamp(end))
        resp = requests.get(
            OPEN_METEO_ARCHIVE,
            params={
                "latitude": ",".join(f"{la:.3f}" for la, _ in cells),
                "longitude": ",".join(f"{lo:.3f}" for _, lo in cells),
                "start_date": f"{chunk_start:%Y-%m-%d}",
                "end_date": f"{chunk_end:%Y-%m-%d}",
                "hourly": "precipitation",
                "models": "era5",
                "timezone": "GMT",
            },
            timeout=600,
        )
        resp.raise_for_status()
        body = resp.json()
        body = body if isinstance(body, list) else [body]
        values = np.nanmean([np.asarray(b["hourly"]["precipitation"], float) for b in body], axis=0)
        times = pd.to_datetime(body[0]["hourly"]["time"], utc=True)
        frames.append(pd.Series(values, index=times))
    series = pd.concat(frames).sort_index()
    series = series[~series.index.duplicated()].rename("precip_mm")
    series.to_frame().to_parquet(path)
    return series


def _gefs_init(ds: xr.Dataset, lat_idx: np.ndarray, lon_idx: np.ndarray, init: pd.Timestamp) -> np.ndarray | None:
    """Ensemble-mean basin precipitation (mm per 3 h step) for one init, leads 0..GEFS_MAX_LEAD_H."""
    try:
        rate = ds["precipitation_surface"].sel(init_time=init.tz_convert(None)).isel(
            lead_time=slice(0, GEFS_MAX_LEAD_H // 3 + 1),
            latitude=slice(int(lat_idx.min()), int(lat_idx.max()) + 1),
            longitude=slice(int(lon_idx.min()), int(lon_idx.max()) + 1),
        ).values
    except KeyError:
        return None
    cells = rate[:, :, lat_idx - lat_idx.min(), lon_idx - lon_idx.min()]  # [member, lead, cell]
    return np.nanmean(cells, axis=(0, 2)) * 3 * 3600.0


def gefs_basin_qpf(site: str, start: str | pd.Timestamp, end: str | pd.Timestamp, workers: int = 16) -> pd.DataFrame:
    """Archived GEFS ensemble-mean basin QPF: one row per 00Z init, columns = lead hours (mm per 3 h ending at lead)."""
    start = max(pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start), GEFS_START).floor("D")
    end = pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end)
    path = _cache_dir() / f"gefs_{site_id(site)}.parquet"
    cached = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    inits = pd.date_range(start, end.floor("D"), freq="D")
    todo = [t for t in inits if cached.empty or t not in cached.index]
    if todo:
        ds = xr.open_zarr(GEFS_ZARR, chunks=None, decode_timedelta=True)
        cells = basin_cells(site)
        lat_idx = np.array([int(np.argmin(np.abs(ds.latitude.values - la))) for la, _ in cells])
        lon_idx = np.array([int(np.argmin(np.abs(ds.longitude.values - lo))) for _, lo in cells])
        leads = (ds.lead_time.values[: GEFS_MAX_LEAD_H // 3 + 1] / np.timedelta64(1, "h")).astype(int)
        with ThreadPoolExecutor(workers) as pool:
            rows = list(pool.map(lambda t: _gefs_init(ds, lat_idx, lon_idx, t), todo))
        fetched = pd.DataFrame([r for r in rows if r is not None], index=pd.DatetimeIndex([t for t, r in zip(todo, rows) if r is not None], name="init_time"), columns=[str(h) for h in leads])
        log.info("GEFS: fetched %d of %d inits", len(fetched), len(todo))
        cached = pd.concat([cached, fetched]).sort_index()
        cached = cached[~cached.index.duplicated(keep="last")]
        cached.to_parquet(path)
    return cached[(cached.index >= start) & (cached.index <= end)]


def forecast_precip(qpf: pd.DataFrame, issue_times: pd.DatetimeIndex, windows_h: list[tuple[float, float]]) -> np.ndarray:
    """Forecast basin precipitation totals (mm) over (issue + a, issue + b] for each window, from the latest
    GEFS init available at issue time. NaN where no init is available. Returns [issue, window]."""
    out = np.full((len(issue_times), len(windows_h)), np.nan)
    if qpf.empty:
        return out
    leads = qpf.columns.astype(int).to_numpy()
    cum = np.nan_to_num(qpf.to_numpy(float), nan=0.0).cumsum(axis=1)  # accumulated precip up to each lead
    inits = qpf.index
    usable = issue_times - pd.Timedelta(hours=GEFS_LATENCY_H)
    pos = inits.searchsorted(usable, side="right") - 1
    for k, (t, p) in enumerate(zip(issue_times, pos)):
        if p < 0 or (t - inits[p]) > pd.Timedelta(days=2):
            continue
        offset = (t - inits[p]).total_seconds() / 3600.0
        for w, (a, b) in enumerate(windows_h):
            lo, hi = offset + a, offset + b
            if hi > leads[-1]:
                continue
            out[k, w] = np.interp(hi, leads, cum[p]) - np.interp(lo, leads, cum[p])
    return out


def observed_precip_windows(precip: pd.Series, issue_times: pd.DatetimeIndex, windows_h: list[tuple[float, float]]) -> np.ndarray:
    """Observed totals (mm) over (issue + a, issue + b] (negative offsets reach into the past). [issue, window]."""
    hourly = precip.reindex(pd.date_range(precip.index.min(), precip.index.max(), freq="h"))
    cum = pd.Series(np.nan_to_num(hourly.to_numpy(), nan=0.0).cumsum(), index=hourly.index)
    valid = hourly.notna()
    out = np.full((len(issue_times), len(windows_h)), np.nan)
    for w, (a, b) in enumerate(windows_h):
        t_lo = (issue_times + pd.Timedelta(hours=a)).floor("h")
        t_hi = (issue_times + pd.Timedelta(hours=b)).floor("h")
        c_lo = cum.reindex(t_lo).to_numpy()
        c_hi = cum.reindex(t_hi).to_numpy()
        ok = valid.reindex(t_hi, fill_value=False).to_numpy() & ~np.isnan(c_lo)
        out[ok, w] = (c_hi - c_lo)[ok]
    return out
