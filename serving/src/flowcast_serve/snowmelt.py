"""Forecast snowmelt estimate (page-data-plan.md §2.5): SNOW-17 reset to SNODAS at issue, run on GEFS.

The method of the WY2021-2022 check (`internal/forecast-snowmelt/melt_forecast.py`), live:
1. open loop over the week before the issue on what the flow model ingests (MRMS precipitation, HRRR analysis where
   MRMS is missing, HRRR analysis temperature, dewpoint, pressure, wind and shortwave), each HRU's pack reset at
   06 UTC to its elevation band's SNODAS SWE (only products published by the issue time, ~13 UTC);
2. from the issue, 168 h on each of the 11 GEFS members: per-band precipitation, temperature, dewpoint and shortwave,
   basin pressure and wind, 3-hourly values held over their step (temperature and dewpoint interpolated);
3. the member-mean basin melt, display-only ("Snowmelt (estimate)").
Parameters are the registry's snow version (the regional NORTHEAST set).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from flowcast_pipeline.snow import HRUSet, prepare_forcing
from flowcast_pipeline.snow.params import HruParams, SnowParams
from flowcast_pipeline.snow.snow17 import (
    ACCMAX,
    AEADJ,
    LIQW,
    N_OUT,
    NEGHS,
    SB,
    SBAESC,
    SBWS,
    SNDPT,
    WE,
    initial_state,
    snow17_kernel,
)

OPEN_LOOP_DAYS = 7
PUBLISH_DELAY = pd.Timedelta(hours=7)  # 06 UTC product, out about 13 UTC
MELT, SWE = 2, 0


def insert_snodas(state: np.ndarray, target: np.ndarray) -> None:
    """Rescale each HRU's pack to SNODAS SWE (direct insertion), keeping its structure where it has one."""
    for j in range(state.shape[0]):
        s = target[j]
        if not np.isfinite(s):
            continue
        m = state[j, WE] + state[j, LIQW]
        if s < 0.1:
            state[j, :] = 0.0
        elif m < 0.1:
            state[j, WE], state[j, LIQW], state[j, NEGHS], state[j, ACCMAX] = s, 0.0, 0.0, s
            state[j, AEADJ] = state[j, SB] = state[j, SBAESC] = state[j, SBWS] = 0.0
            state[j, SNDPT] = 0.4 * s  # cm, density 0.25
        else:
            r = s / m
            for i in (WE, LIQW, NEGHS, SNDPT, SB, SBWS):
                state[j, i] *= r
            state[j, ACCMAX] = max(state[j, ACCMAX] * r, s)


def _run(f: dict, hp: HruParams, state: np.ndarray, sl: slice) -> np.ndarray:
    nt = sl.stop - sl.start
    out = np.zeros((N_OUT, nt, state.shape[0]), dtype=np.float32)

    def a(k):
        return np.ascontiguousarray(f[k][sl])

    snow17_kernel(a("ta"), a("px"), a("fracs"), a("ea"), a("pa_mb"), a("wind"), a("sw"), np.ascontiguousarray(f["idn"][sl]),
                  f["step_hours"], hp.values, hp.adc, hp.flags, state, out)
    return out


def estimate(hrus: HRUSet, params: SnowParams, basin_elev: float, band_elev: np.ndarray, analysis: pd.DataFrame, snodas_daily: pd.DataFrame,
             gefs_basin: np.ndarray, gefs_bands: np.ndarray, leads: np.ndarray, init: pd.Timestamp, issue: pd.Timestamp, horizon_h: int = 168) -> dict:
    """Hourly member-mean basin melt (mm) for issue+1..issue+H, the 6 h bins, and basin SWE at the issue (model state).

    `analysis` is hourly (UTC) with hrrr_an_* and mrms_precip_mm_h; `snodas_daily` has day and swe_mm_band{k};
    `gefs_basin[member, lead, var]` and `gefs_bands[band, member, lead, var]` use the GEFS output order.
    """
    band = hrus.table["band"].to_numpy() - 1
    w = hrus.area_weights()
    hp = HruParams.build(params, hrus.table["lat"].to_numpy(), hrus.table["forest_frac"].to_numpy())
    start = (issue - pd.Timedelta(days=OPEN_LOOP_DAYS)).floor("D") + pd.Timedelta(hours=6)
    a = analysis.loc[(analysis.index > start - pd.Timedelta(hours=1)) & (analysis.index <= issue)]
    mrms = a["mrms_precip_mm_h"]
    forcing = pd.DataFrame({
        "precip": mrms.where(np.isfinite(mrms), a["hrrr_an_precip_mm_h"]).fillna(0.0),
        "air_temperature": a["hrrr_an_temp_2m_c"].interpolate(limit_direction="both"),
        "dewpoint_temperature": a["hrrr_an_dewpoint_2m_c"].interpolate(limit_direction="both"),
        "surface_pressure": a["hrrr_an_pressure_kpa"].interpolate(limit_direction="both") * 1000.0,
        "wind_speed": a["hrrr_an_wind_speed_10m"].interpolate(limit_direction="both"),
        "shortwave_down": a["hrrr_an_sw_down_wm2"].fillna(0.0),
    })
    forcing.index = forcing.index.tz_convert(None)
    f = prepare_forcing(forcing, hrus, params, forcing_elevation=basin_elev, label="end")
    times = pd.DatetimeIndex(f["times"])
    state = initial_state(hrus.n)
    sn = snodas_daily.set_index(pd.DatetimeIndex(snodas_daily["day"]))
    issue_n = issue.tz_convert(None)
    pos = 0
    for i, t in enumerate(times):
        if t.hour == 6 and t + PUBLISH_DELAY <= issue_n and t.normalize() in sn.index:
            if i > pos:
                _run(f, hp, state, slice(pos, i))
                pos = i
            insert_snodas(state, sn.loc[t.normalize(), [f"swe_mm_band{b}" for b in band]].to_numpy(float))
    if pos < len(times):
        _run(f, hp, state, slice(pos, len(times)))
    swe0 = float((state[:, WE] + state[:, LIQW]) @ w)

    offset = (issue - init) / pd.Timedelta(hours=1)
    H = int(min(horizon_h, leads[-1] - offset))
    hours = offset + np.arange(1, H + 1)
    k = np.searchsorted(leads, hours)
    vt = issue_n + pd.to_timedelta(np.arange(1, H + 1), unit="h")
    z = band_elev[band].astype(np.float64)
    melt = np.zeros((gefs_basin.shape[0], H))
    for m in range(gefs_basin.shape[0]):
        bands = gefs_bands[:, m]  # [band, lead, var]

        def per_hru(j):
            return np.nan_to_num(bands[:, k, j].T[:, band].astype(np.float64))

        def interp(j):
            return np.stack([np.interp(hours, leads, bands[b, :, j]) for b in band], axis=1)

        data = {
            "precip": per_hru(0), "air_temperature": interp(1), "dewpoint_temperature": interp(2), "shortwave_down": per_hru(5),
            "surface_pressure": np.repeat(gefs_basin[m, k, 3:4].astype(np.float64) * 1000.0, hrus.n, axis=1),
            "wind_speed": np.repeat(gefs_basin[m, k, 4:5].astype(np.float64), hrus.n, axis=1),
        }
        ds = xr.Dataset({n: (("time", "hru"), v) for n, v in data.items()}, coords={"time": vt, "hru": hrus.ids})
        fg = prepare_forcing(ds, hrus, params, forcing_elevation=z, label="end")
        out = _run(fg, hp, state.copy(), slice(0, H))
        melt[m] = out[MELT] @ w
    hourly = melt.mean(axis=0)
    bins = [float(hourly[i : i + 6].sum()) for i in range(0, H - H % 6, 6)]
    return {"hourly_mm": hourly, "bins_6h_mm": bins, "swe_model_mm": swe0, "hours": H}
