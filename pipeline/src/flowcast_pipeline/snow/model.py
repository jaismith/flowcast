"""Run the snow and radiation module for an HRU set.

Forcing uses canonical names (see FORCING_VARS). It can be basin-level (a DataFrame indexed by time, or an
xarray Dataset with only a `time` dimension), in which case it is broadcast to HRUs with lapse-rate adjustments, or
per HRU (an xarray Dataset with dims (time, hru) whose `hru` coordinate holds HRU ids).
"""

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
import xarray as xr

from . import meteo
from .hru import HRUSet
from .params import NORTHEAST, HruParams, SnowParams
from .radiation import terrain_shortwave
from .snow17 import N_OUT, OUTPUTS, day_number_from_march21, initial_state, snow17_kernel

FORCING_VARS = {
    "precip": "mm per time step, accumulated over the interval ending at the time label",
    "air_temperature": "degC (2 m)",
    "specific_humidity": "kg/kg (2 m); or `dewpoint_temperature` in degC",
    "surface_pressure": "Pa",
    "wind_speed": "m/s (10 m); or `u_wind` and `v_wind`",
    "shortwave_down": "W/m2, flat-surface global horizontal irradiance, mean over the interval",
}

AORC_NAMES = {
    "APCP_surface": "precip",
    "TMP_2maboveground": "air_temperature",
    "SPFH_2maboveground": "specific_humidity",
    "PRES_surface": "surface_pressure",
    "UGRD_10maboveground": "u_wind",
    "VGRD_10maboveground": "v_wind",
    "DSWRF_surface": "shortwave_down",
    "DLWRF_surface": "longwave_down",
}

# AORC shortwave is centered on its time stamp; precipitation is accumulated over the hour ending at it.
AORC_RADIATION_LABEL = "center"

DEFAULT_WIND_MS = 3.0
DEFAULT_RH = 0.9
MAX_TEMPERATURE_GAP_H = 6


def forcing_from_aorc(forcing: pd.DataFrame | xr.Dataset) -> pd.DataFrame | xr.Dataset:
    """Rename AORC variables to canonical names and convert temperature from K to degC."""
    names = {k: v for k, v in AORC_NAMES.items() if k in forcing}
    out = forcing.rename(columns=names) if isinstance(forcing, pd.DataFrame) else forcing.rename(names)
    out["air_temperature"] = out["air_temperature"] - 273.15
    return out


@dataclass
class SnowResult:
    """Per-HRU hourly outputs (`hru`, dims time x hru) and the end-of-run model state for warm starts."""

    hru: xr.Dataset
    state: np.ndarray
    hrus: HRUSet
    params: SnowParams


def _as_time_by_hru(forcing, name: str, hrus: HRUSet, per_hru: bool) -> np.ndarray | None:
    if name not in forcing:
        return None
    if isinstance(forcing, pd.DataFrame):
        return forcing[name].to_numpy(dtype=np.float64)
    da = forcing[name]
    if per_hru:
        return da.transpose("time", "hru").sel(hru=hrus.ids).to_numpy().astype(np.float64)
    return da.to_numpy().astype(np.float64)


def _time_index(forcing) -> pd.DatetimeIndex:
    t = forcing.index if isinstance(forcing, pd.DataFrame) else forcing.indexes["time"]
    t = pd.DatetimeIndex(t)
    t = t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t
    return t.as_unit("ns")


def prepare_forcing(
    forcing: pd.DataFrame | xr.Dataset,
    hrus: HRUSet,
    params: SnowParams = NORTHEAST,
    forcing_elevation: float | np.ndarray | None = None,
    label: str = "end",
    radiation_label: str | None = None,
) -> dict:
    """Canonical forcing -> (time, hru) kernel inputs, including the precipitation phase and terrain shortwave.

    `label` is where time stamps sit in each accumulation interval ("end" or "start"); `radiation_label` overrides
    it for shortwave ("end", "start" or "center").
    """
    times = _time_index(forcing)
    if len(times) < 2:
        raise ValueError("need at least two time steps")
    steps = np.diff(times.asi8)
    if not np.all(steps == steps[0]) or steps[0] % 3_600_000_000_000 != 0:
        raise ValueError("forcing must be on a regular whole-hour time step")
    step_h = int(steps[0] // 3_600_000_000_000)
    per_hru = isinstance(forcing, xr.Dataset) and "hru" in forcing.dims
    nt, nh = len(times), hrus.n
    z_hru = hrus.table["elev_mean"].to_numpy(dtype=np.float64)
    if forcing_elevation is None:
        z_ref = z_hru if per_hru else np.full(nh, hrus.reference_elevation())
    else:
        z_ref = np.broadcast_to(np.asarray(forcing_elevation, dtype=np.float64), (nh,))
    dz = (z_hru - z_ref)[None, :]

    def grab(name):
        arr = _as_time_by_hru(forcing, name, hrus, per_hru)
        if arr is None:
            return None
        return np.broadcast_to(arr[:, None], (nt, nh)).copy() if arr.ndim == 1 else arr

    t_ref = grab("air_temperature")
    if t_ref is None:
        raise ValueError("air_temperature is required")
    t_ref = pd.DataFrame(t_ref).ffill(limit=MAX_TEMPERATURE_GAP_H).bfill(limit=MAX_TEMPERATURE_GAP_H).to_numpy()
    if np.isnan(t_ref).any():
        raise ValueError("air_temperature has gaps longer than %d h" % MAX_TEMPERATURE_GAP_H)
    ta = t_ref + params.temp_lapse_c_per_km * dz / 1000.0

    p_ref = grab("surface_pressure")
    if p_ref is None or np.isnan(p_ref).all():
        pa_pa = np.broadcast_to(meteo.standard_pressure_pa(z_hru)[None, :], (nt, nh)).copy()
        p_ref = np.broadcast_to(meteo.standard_pressure_pa(z_ref)[None, :], (nt, nh))
    else:
        p_ref = np.where(np.isfinite(p_ref), p_ref, meteo.standard_pressure_pa(z_ref)[None, :])
        pa_pa = meteo.pressure_at_elevation(p_ref, t_ref, dz)

    q = grab("specific_humidity")
    td = grab("dewpoint_temperature")
    if q is not None:
        e_ref = meteo.vapor_pressure_from_specific_humidity(q, p_ref)
    elif td is not None:
        e_ref = meteo.vapor_pressure_from_dewpoint(td)
    else:
        e_ref = np.full((nt, nh), np.nan)
    rh = np.clip(e_ref / meteo.esat_mb(t_ref), 0.0, 1.0)
    rh = np.where(np.isfinite(rh), rh, DEFAULT_RH)
    ea = rh * meteo.esat_mb(ta)

    wind = grab("wind_speed")
    if wind is None:
        u, v = grab("u_wind"), grab("v_wind")
        wind = meteo.wind_speed(u, v) if u is not None and v is not None else np.full((nt, nh), np.nan)
    if np.isnan(wind).any():
        warnings.warn("missing wind filled with %.1f m/s" % DEFAULT_WIND_MS, stacklevel=2)
        wind = np.where(np.isfinite(wind), wind, DEFAULT_WIND_MS)

    px = grab("precip")
    if px is None:
        raise ValueError("precip is required")
    px = np.where(np.isfinite(px), np.maximum(px, 0.0), 0.0)
    if params.precip_gradient_per_km and not per_hru:
        factor = np.maximum(1.0 + params.precip_gradient_per_km * dz[0] / 1000.0, 0.0)
        factor = factor / np.sum(hrus.area_weights() * factor)
        px = px * factor[None, :]

    tw = meteo.wet_bulb(ta, ea, pa_pa)
    fracs = meteo.snow_fraction(ta, tw, params)

    ghi = grab("shortwave_down")
    rad = terrain_shortwave(times, ghi, hrus, step_hours=step_h, label=radiation_label or label)
    mid = times - pd.Timedelta(hours=step_h / 2) if label == "end" else times + pd.Timedelta(hours=step_h / 2)
    return {
        "times": times,
        "step_hours": step_h,
        "ta": np.ascontiguousarray(ta),
        "px": np.ascontiguousarray(px),
        "fracs": np.ascontiguousarray(fracs),
        "ea": np.ascontiguousarray(ea),
        "pa_mb": np.ascontiguousarray(pa_pa / 100.0),
        "wind": np.ascontiguousarray(wind),
        "sw": np.ascontiguousarray(rad["sw_terrain"]),
        "sw_clear": rad["sw_clear_terrain"],
        "wet_bulb": tw,
        "idn": day_number_from_march21(mid),
    }


def run_snow(
    hrus: HRUSet,
    forcing: pd.DataFrame | xr.Dataset,
    params: SnowParams = NORTHEAST,
    state: np.ndarray | None = None,
    forcing_elevation: float | np.ndarray | None = None,
    label: str = "end",
    radiation_label: str | None = None,
) -> SnowResult:
    """Run SNOW-17 with terrain-corrected shortwave for every HRU. Start in late summer (no snow) or pass `state`."""
    f = prepare_forcing(forcing, hrus, params, forcing_elevation, label, radiation_label)
    nt, nh = f["ta"].shape
    hp = HruParams.build(params, hrus.table["lat"].to_numpy(), hrus.table["forest_frac"].to_numpy())
    st = initial_state(nh) if state is None else np.array(state, dtype=np.float64, copy=True)
    out = np.zeros((N_OUT, nt, nh), dtype=np.float32)
    snow17_kernel(
        f["ta"], f["px"], f["fracs"], f["ea"], f["pa_mb"], f["wind"], f["sw"], f["idn"], f["step_hours"],
        hp.values, hp.adc, hp.flags, st, out,
    )  # fmt: skip
    coords = {
        "time": f["times"],
        "hru": hrus.ids,
        "subbasin": ("hru", hrus.table["subbasin_id"].tolist()),
        "band_id": ("hru", hrus.table["band_id"].tolist()),
        "band": ("hru", hrus.table["band"].to_numpy()),
        "elevation": ("hru", hrus.table["elev_mean"].to_numpy()),
        "area_km2": ("hru", hrus.table["area_km2"].to_numpy()),
    }
    data = {name: (("time", "hru"), out[k]) for k, name in enumerate(OUTPUTS)}
    data["sw_terrain"] = (("time", "hru"), f["sw"].astype(np.float32))
    data["sw_clear_terrain"] = (("time", "hru"), f["sw_clear"].astype(np.float32))
    data["air_temperature"] = (("time", "hru"), f["ta"].astype(np.float32))
    data["wet_bulb"] = (("time", "hru"), f["wet_bulb"].astype(np.float32))
    data["snow_frac_precip"] = (("time", "hru"), f["fracs"].astype(np.float32))
    ds = xr.Dataset(data, coords=coords, attrs={"step_hours": f["step_hours"], "time_label": label})
    return SnowResult(ds, st, hrus, params)
