"""Water-temperature training cube: the temperature basins of a training cube plus derived inputs (plan §3).

Reads the source cube's `trainval.zarr` only (never the frozen test store) and writes a smaller store with:

* the basins that have any water temperature (`tw_c`) in trainval;
* the hourly forcings, discharge, gauged dam outflow and archived GEFS forecasts the temperature model reads
  (operational GEFS trimmed to members 0-4, like the flow model's hindcasts);
* every numeric `(basin,)` attribute;
* derived hourly inputs: day-of-year and local solar-hour harmonics, water temperature at the largest upstream
  gauge that records it (`upstream_tw_c`), and the mean water temperature of the gauged dam-release sites
  (`outflow_tw_c`), each NaN where a basin has none, with per-basin flags and the upstream gauge's travel time;
* optionally `flowfc_qobs_mm_h (basin, flowfc_init, flowfc_member, flowfc_lead)`, streamflow-model forecasts from
  its validation hindcasts: per GEFS member, the median of every CMAL draw of every run, interpolated to hourly
  leads. Hindcasts are issued at 00/06/12/18Z from WY2021; member m used GEFS member m.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import zarr

from .units import cfs_to_mm_h

log = logging.getLogger(__name__)

DYNAMIC = [
    "tw_c",
    "qobs_mm_h",
    "gauged_outflow_mm_h",
    *[f"aorc_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2", "spfh_2m_gkg")],
    *[f"hrrr_an_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2")],
    "mrms_precip_mm_h",
]
GEFS_VARS = ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2")
GEFS_MEMBERS = 5
FLOW_LEADS_H = np.arange(1, 169)


def temperature_basins(ds: xr.Dataset) -> list[int]:
    tw = ds["tw_c"].values
    return [int(i) for i in np.flatnonzero(np.isfinite(tw).any(axis=1))]


def time_harmonics(times: pd.DatetimeIndex, lon: np.ndarray) -> dict[str, np.ndarray]:
    """Day-of-year and local solar-hour harmonics, (basin, time); values are hour-ending, so the mid-hour is used."""
    mid = times - pd.Timedelta(minutes=30)
    doy = (mid.dayofyear.to_numpy() - 1 + mid.hour.to_numpy() / 24.0) / 365.25
    solar = (mid.hour.to_numpy()[None, :] + 0.5 + np.asarray(lon, float)[:, None] / 15.0) / 24.0
    n = len(lon)
    return {
        "doy_sin": np.broadcast_to(np.sin(2 * np.pi * doy), (n, len(times))).astype(np.float32),
        "doy_cos": np.broadcast_to(np.cos(2 * np.pi * doy), (n, len(times))).astype(np.float32),
        "solar_sin": np.sin(2 * np.pi * solar).astype(np.float32),
        "solar_cos": np.cos(2 * np.pi * solar).astype(np.float32),
    }


def _sites(value) -> list[str]:
    return [s for s in str(value).replace(",", " ").split() if s.isdigit()]


def gauge_temperatures(ds: xr.Dataset, idx: list[int], train_end: pd.Timestamp) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """`upstream_tw_c` and `outflow_tw_c` for basins `idx`, with their flags and the upstream gauge's travel time."""
    basins = [str(b) for b in ds["basin"].values]
    pos = {b: i for i, b in enumerate(basins)}
    tw = ds["tw_c"].values
    train = pd.DatetimeIndex(ds["time"].values) < train_end
    train_hours = np.isfinite(tw[:, train]).sum(axis=1)
    slots = ds["upstream_slot_site"].values
    travel = ds["upstream_slot_travel_time_h"].values
    area = ds["upstream_slot_area_frac"].values
    outflow = ds["gauged_outflow_sites"].values
    n, T = len(idx), tw.shape[1]
    up = np.full((n, T), np.nan, dtype=np.float32)
    out = np.full((n, T), np.nan, dtype=np.float32)
    static = {k: np.zeros(n) for k in ("has_upstream_tw", "upstream_tw_travel_time_h", "upstream_tw_area_frac", "has_outflow_tw", "outflow_tw_n")}
    for j, i in enumerate(idx):
        best, best_hours = None, 0
        for k in range(slots.shape[1]):
            g = pos.get(str(slots[i, k]))
            if g is not None and g != i and train_hours[g] > best_hours:
                best, best_hours = (g, k), train_hours[g]
        if best is not None:
            g, k = best
            up[j] = tw[g]
            static["has_upstream_tw"][j] = 1.0
            static["upstream_tw_travel_time_h"][j] = float(np.nan_to_num(travel[i, k]))
            static["upstream_tw_area_frac"][j] = float(np.nan_to_num(area[i, k]))
        gauges = [pos[s] for s in _sites(outflow[i]) if s in pos and pos[s] != i and np.isfinite(tw[pos[s]]).any()]
        if gauges:
            with np.errstate(all="ignore"):
                out[j] = np.nanmean(tw[gauges], axis=0)
            static["has_outflow_tw"][j] = 1.0
            static["outflow_tw_n"][j] = float(len(gauges))
    return {"upstream_tw_c": up, "outflow_tw_c": out}, static


def flow_forecasts(hindcast_roots: list[str], basins: list[str], areas: dict[str, float], model: str, issue_hours=(0, 6, 12, 18), n_members: int = GEFS_MEMBERS) -> xr.DataArray:
    """Streamflow-model forecasts as a forecast product (mm/h), from one or more runs' hindcast directories."""
    per_basin: dict[str, pd.DataFrame] = {}
    for b in basins:
        frames = []
        for root in hindcast_roots:
            path = f"{root.rstrip('/')}/site_id=USGS-{b}/{model}.parquet"
            try:
                df = pd.read_parquet(path, columns=["issue_time", "lead_h", "member", "value"])
            except (FileNotFoundError, OSError):
                continue
            frames.append(df)
        if frames:
            per_basin[b] = pd.concat(frames, ignore_index=True)
    if not per_basin:
        raise ValueError("no flow hindcasts found")
    inits = sorted({t for df in per_basin.values() for t in df["issue_time"].unique()})
    inits = pd.DatetimeIndex(inits).tz_convert(None) if pd.DatetimeIndex(inits).tz is not None else pd.DatetimeIndex(inits)
    inits = inits[inits.hour.isin(issue_hours) & (inits.minute == 0)]
    values = np.full((len(basins), len(inits), n_members, len(FLOW_LEADS_H)), np.nan, dtype=np.float32)
    for j, b in enumerate(basins):
        df = per_basin.get(b)
        if df is None:
            continue
        df = df.assign(issue_time=pd.DatetimeIndex(df["issue_time"]).tz_convert(None) if pd.DatetimeIndex(df["issue_time"]).tz is not None else df["issue_time"])
        df = df[df["issue_time"].isin(inits)]
        # hindcast member = GEFS member x n_samples + CMAL draw
        n_draws = int(df["member"].max() + 1) // n_members
        df = df.assign(gefs=(df["member"] // n_draws).astype(int))
        med = df.groupby(["issue_time", "gefs", "lead_h"])["value"].median()
        grid_leads = np.sort(med.index.get_level_values("lead_h").unique().to_numpy(float))
        cube = med.unstack("lead_h").reindex(columns=grid_leads)
        cube = cube.reindex(pd.MultiIndex.from_product([inits, range(n_members)], names=["issue_time", "gefs"]))
        arr = cube.to_numpy(float)
        hourly = np.full((arr.shape[0], len(FLOW_LEADS_H)), np.nan)
        ok = np.isfinite(arr).all(axis=1)
        for r in np.flatnonzero(ok):
            hourly[r] = np.interp(FLOW_LEADS_H, grid_leads, arr[r])
        values[j] = cfs_to_mm_h(hourly, areas[b]).reshape(len(inits), n_members, len(FLOW_LEADS_H))
    return xr.DataArray(
        values,
        dims=("basin", "flowfc_init", "flowfc_member", "flowfc_lead"),
        coords={"basin": np.array(basins, dtype=str), "flowfc_init": inits.values, "flowfc_member": np.arange(n_members), "flowfc_lead": FLOW_LEADS_H.astype(float)},
        attrs={"units": "mm/h", "source": ", ".join(hindcast_roots), "model": model},
    )


def _write(out: Path, name: str, da: xr.DataArray) -> None:
    """Append one variable, chunked one basin at a time (the streaming dataset reads a basin's full series)."""
    chunks = tuple(1 if d == "basin" else s for d, s in zip(da.dims, da.shape))
    existing = set(zarr.open_group(str(out), mode="r").array_keys())
    ds = da.to_dataset(name=name).drop_vars([c for c in da.coords if c in existing and c != name])
    ds.to_zarr(str(out), mode="a", encoding={name: {"chunks": chunks}}, consolidated=False, zarr_format=3)


def build(source: str, out: str | Path, flow_hindcasts: list[str] | None = None, flow_model: str = "lstm_full_v2_tt", train_end: str = "2019-10-01", max_basins: int | None = None) -> Path:
    if Path(source).name == "test.zarr" or source.rstrip("/").endswith("test.zarr"):
        raise ValueError("the temperature cube is built from trainval only; the frozen test store is never read")
    src = xr.open_zarr(source, chunks=None, consolidated=None)
    out = Path(out)
    idx = temperature_basins(src)[:max_basins]
    basins = [str(b) for b in src["basin"].values[idx]]
    times = pd.DatetimeIndex(src["time"].values)
    log.info("%d temperature basins", len(idx))
    static_names = [v for v, da in src.data_vars.items() if da.dims == ("basin",) and np.issubdtype(da.dtype, np.number)]
    statics = src[static_names].isel(basin=idx).load()
    statics = statics.assign_coords(basin=np.array(basins, dtype=str))
    statics.attrs = dict(src.attrs) | {"flowcast_subset": "water-temperature basins (tw_c in trainval)", "source": source}
    enc = {v: {"chunks": (len(basins),)} for v in static_names} | {"basin": {"chunks": (len(basins),)}}
    statics.to_zarr(str(out), mode="w", encoding=enc, consolidated=False, zarr_format=3)
    coords = {"basin": np.array(basins, dtype=str), "time": times.values}
    for v in DYNAMIC:
        log.info("dynamic %s", v)
        da = src[v].isel(basin=idx).load().assign_coords(basin=coords["basin"])
        _write(out, v, da)
    lon = statics["lon"].values
    for name, arr in time_harmonics(times, lon).items():
        _write(out, name, xr.DataArray(arr, dims=("basin", "time"), coords=coords))
    derived, flags = gauge_temperatures(src, idx, pd.Timestamp(train_end))
    for name, arr in derived.items():
        _write(out, name, xr.DataArray(arr, dims=("basin", "time"), coords=coords))
    for name, arr in flags.items():
        _write(out, name, xr.DataArray(arr, dims=("basin",), coords={"basin": coords["basin"]}))
    for v in GEFS_VARS:
        for name, members in ((f"gefs_rf_{v}", None), (f"gefs_{v}", slice(0, GEFS_MEMBERS))):
            log.info("forecast %s", name)
            da = src[name]
            sel = {"basin": idx}
            if members is not None:
                sel["gefs_member"] = members
            _write(out, name, da.isel(sel).load().assign_coords(basin=coords["basin"]))
    if flow_hindcasts:
        log.info("flow forecasts from %s", flow_hindcasts)
        areas = dict(zip(basins, statics["area_km2"].values.astype(float)))
        _write(out, "flowfc_qobs_mm_h", flow_forecasts(flow_hindcasts, basins, areas, flow_model))
    zarr.consolidate_metadata(str(out))
    return out
