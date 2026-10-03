"""Water-temperature training cube: the temperature basins of a training cube plus derived inputs (plan §3).

Reads the source cube's `trainval.zarr` only (never the frozen test store) and writes a smaller store with:

* the basins that have any water temperature (`tw_c`) in trainval;
* the hourly forcings, discharge, gauged dam outflow, SNODAS snowpack and archived GEFS forecasts the temperature
  model reads (operational GEFS trimmed to the first `gefs_members` members);
* every numeric `(basin,)` attribute;
* derived hourly inputs: day-of-year and local solar-hour harmonics, water temperature at the largest upstream
  gauge that records it (`upstream_tw_c`), and the mean water temperature of the gauged dam-release sites
  (`outflow_tw_c`), each NaN where a basin has none, with per-basin flags and the upstream gauge's travel time;
* derived forecast inputs `gefs[_rf]_warmup_max_c` / `_mean_c`: each GEFS member's air-temperature warm-up, the 24 h
  running max (mean) ending at each lead minus the same over the init's first 24 h (`forecast_warmup`);
* `heat_weight (basin, time)`: a training-loss weight that is larger on warm-up days and heat-wave onsets
  (`heat_weight`), from AORC air temperature;
* optionally `flowfc_qobs_mm_h (basin, flowfc_init, flowfc_member, flowfc_lead)`, streamflow-model forecasts from
  its validation hindcasts: per GEFS member, the median of every CMAL draw of every run, interpolated to hourly
  leads. Hindcasts are issued at 00/06/12/18Z from WY2021; member m used GEFS member m.
"""

from __future__ import annotations

import logging
import warnings
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
    "snodas_swe_mm",
    "snodas_band_swe_mm",
]
GEFS_VARS = ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2")
GEFS_MEMBERS = 11
FLOW_LEADS_H = np.arange(1, 169)
WARMUP_WINDOW_H = 24.0
# Heat-wave definition of the miss diagnostics: >= 3 days above the basin's calendar-day 90th percentile of AORC
# daily-max air temperature (training years, +-7-day window); its first 3 days are the onset.
HEAT_TZ = "America/New_York"
HEAT_MONTHS = (5, 6, 7, 8, 9)


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
    # per basin, reduced to the per-member median as it is read (raw 44-member hindcasts of 126 basins don't fit in memory)
    medians: dict[str, pd.Series] = {}
    for b in basins:
        frames = []
        for root in hindcast_roots:
            path = f"{root.rstrip('/')}/site_id=USGS-{b}/{model}.parquet"
            try:
                df = pd.read_parquet(path, columns=["issue_time", "lead_h", "member", "value"])
            except (FileNotFoundError, OSError):
                continue
            issued = pd.DatetimeIndex(df["issue_time"])
            issued = issued.tz_convert(None) if issued.tz is not None else issued
            df = df.assign(issue_time=issued)[issued.hour.isin(issue_hours) & (issued.minute == 0)]
            # hindcast member = GEFS member x n_samples + CMAL draw
            n_draws = int(df["member"].max() + 1) // n_members
            frames.append(df.assign(gefs=(df["member"] // n_draws).astype(int)).drop(columns="member"))
        if frames:
            medians[b] = pd.concat(frames, ignore_index=True).groupby(["issue_time", "gefs", "lead_h"])["value"].median()
            log.info("flow forecasts %s: %d runs", b, len(frames))
    if not medians:
        raise ValueError("no flow hindcasts found")
    inits = pd.DatetimeIndex(sorted({t for med in medians.values() for t in med.index.get_level_values("issue_time").unique()}))
    values = np.full((len(basins), len(inits), n_members, len(FLOW_LEADS_H)), np.nan, dtype=np.float32)
    for j, b in enumerate(basins):
        med = medians.get(b)
        if med is None:
            continue
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


def forecast_warmup(temp: xr.DataArray, lead_dim: str, window_h: float = WARMUP_WINDOW_H) -> dict[str, xr.DataArray]:
    """Forecast air-temperature warm-up per init, member and lead (the model-input version of the warm-up feature
    of `calibrate.TempWarmupCalibration`).

    `warmup_max`: the forecast's running max over the `window_h` hours ending at each lead minus its max over the
    init's first `window_h` hours (lead 0, the analysis, excluded); `warmup_mean` the same with means. Leads up to
    `window_h` use the first window, so their warm-up is 0. NaN where a window has no data.
    """
    leads = temp[lead_dim].values.astype(float)
    axis = temp.dims.index(lead_dim)
    arr = np.moveaxis(temp.values.astype(np.float32), axis, -1)
    first = (leads > 0) & (leads <= window_h)
    out = {k: np.full(arr.shape, np.nan, dtype=np.float32) for k in ("warmup_max", "warmup_mean")}
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ref = {"warmup_max": np.nanmax(arr[..., first], axis=-1), "warmup_mean": np.nanmean(arr[..., first], axis=-1)}
        for j, lead in enumerate(leads):
            end = max(lead, window_h)
            win = (leads > max(end - window_h, 0.0)) & (leads <= end)
            out["warmup_max"][..., j] = np.nanmax(arr[..., win], axis=-1) - ref["warmup_max"]
            out["warmup_mean"][..., j] = np.nanmean(arr[..., win], axis=-1) - ref["warmup_mean"]
    return {k: temp.copy(data=np.moveaxis(v, -1, axis)) for k, v in out.items()}


def _local_daily_max(air: np.ndarray, times: pd.DatetimeIndex, tz: str = HEAT_TZ, min_hours: int = 20) -> tuple[np.ndarray, pd.DatetimeIndex, np.ndarray]:
    """Daily max per basin of hour-ending values by local date (NaN with fewer than `min_hours` hours), the dates,
    and each hour's date position."""
    local = (times - pd.Timedelta(hours=1)).tz_localize("UTC").tz_convert(tz).tz_localize(None).normalize()
    days, pos = np.unique(local.values, return_inverse=True)
    n_days = len(days)
    tmax = np.full((air.shape[0], n_days), -np.inf)
    count = np.zeros((air.shape[0], n_days))
    for j in range(air.shape[0]):
        ok = np.isfinite(air[j])
        np.maximum.at(tmax[j], pos[ok], air[j, ok])
        np.add.at(count[j], pos[ok], 1)
    tmax[count < min_hours] = np.nan
    return tmax, pd.DatetimeIndex(days), pos


def heat_weight(air: np.ndarray, times: pd.DatetimeIndex, train_end: pd.Timestamp, ramp_c: tuple[float, float] = (1.0, 5.0), max_weight: float = 3.0) -> np.ndarray:
    """Per-hour training-loss weight (basin, time) for warm-up periods, from AORC air temperature.

    A May-Sep day's weight rises linearly from 1 to `max_weight` as its daily-max air temperature exceeds the mean
    of the previous 3 days' by `ramp_c[0]` to `ramp_c[1]` degC, and is `max_weight` on the first 3 days of a heat
    wave (`HEAT_TZ` local days; percentiles from the days before `train_end`). Other days and hours without data
    weigh 1.
    """
    tmax, days, pos = _local_daily_max(air, times)
    prior = pd.DataFrame(tmax.T).rolling(3, min_periods=2).mean().shift(1).to_numpy().T
    rise = (tmax - prior - ramp_c[0]) / (ramp_c[1] - ramp_c[0])
    level = np.clip(np.nan_to_num(rise, nan=0.0), 0.0, 1.0)
    doy = days.dayofyear.to_numpy()
    train = days < train_end
    gap = np.abs(doy[None, :] - np.arange(367)[:, None])
    near = (np.minimum(gap, 366 - gap) <= 7) & train[None, :]
    for j in range(tmax.shape[0]):
        p90 = np.full(367, np.nan)
        for d in range(1, 367):
            sel = near[d] & np.isfinite(tmax[j])
            if sel.sum() >= 30:
                p90[d] = np.quantile(tmax[j, sel], 0.9)
        hot = np.nan_to_num(tmax[j] > p90[doy], nan=0).astype(bool)
        run = np.zeros(len(hot), dtype=int)
        for i in range(len(hot)):
            run[i] = run[i - 1] + 1 if hot[i] and i > 0 else int(hot[i])
        onset = np.zeros(len(hot), dtype=bool)
        for i in np.flatnonzero(run == 3):
            onset[i - 2 : i + 1] = True
        level[j, onset] = 1.0
    level[:, ~np.isin(days.month, HEAT_MONTHS)] = 0.0
    daily = 1.0 + (max_weight - 1.0) * level
    return daily[:, pos].astype(np.float32)


def _write(out: Path, name: str, da: xr.DataArray) -> None:
    """Append one variable, chunked one basin at a time (the streaming dataset reads a basin's full series)."""
    chunks = tuple(1 if d == "basin" else s for d, s in zip(da.dims, da.shape))
    existing = set(zarr.open_group(str(out), mode="r").array_keys())
    ds = da.to_dataset(name=name).drop_vars([c for c in da.coords if c in existing and c != name])
    ds.to_zarr(str(out), mode="a", encoding={name: {"chunks": chunks}}, consolidated=False, zarr_format=3)


def build(source: str, out: str | Path, flow_hindcasts: list[str] | None = None, flow_model: str = "lstm_full_v2_tt", train_end: str = "2019-10-01", max_basins: int | None = None, gefs_members: int = GEFS_MEMBERS) -> Path:
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
    log.info("heat_weight")
    air = src["aorc_temp_2m_c"].isel(basin=idx).values
    _write(out, "heat_weight", xr.DataArray(heat_weight(air, times, pd.Timestamp(train_end)), dims=("basin", "time"), coords=coords))
    del air
    for v in GEFS_VARS:
        for prefix, members in (("gefs_rf", None), ("gefs", slice(0, gefs_members))):
            name = f"{prefix}_{v}"
            log.info("forecast %s", name)
            sel = {"basin": idx}
            if members is not None:
                sel["gefs_member"] = members
            da = src[name].isel(sel).load().assign_coords(basin=coords["basin"])
            _write(out, name, da)
            if v == "temp_2m_c":
                for kind, warm in forecast_warmup(da, f"{prefix}_lead").items():
                    _write(out, f"{prefix}_{kind}_c", warm)
            del da
    if flow_hindcasts:
        log.info("flow forecasts from %s", flow_hindcasts)
        areas = dict(zip(basins, statics["area_km2"].values.astype(float)))
        _write(out, "flowfc_qobs_mm_h", flow_forecasts(flow_hindcasts, basins, areas, flow_model, n_members=gefs_members))
    zarr.consolidate_metadata(str(out))
    return out
