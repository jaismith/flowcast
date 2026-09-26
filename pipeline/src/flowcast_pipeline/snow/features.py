"""LSTM input features and mappable states from snow-module results.

Basin features are area-weighted means over all HRUs. Band features aggregate HRUs by band index across
sub-basins (band 1 = lowest), so with equal-area bands every basin gets the same feature width.
"""

import numpy as np
import pandas as pd
import xarray as xr

from .model import SnowResult

N_FEATURE_BANDS = 4

BASIN_FEATURES = {
    "snow_swe": ("swe", "mm", "snow water equivalent including liquid water in the pack"),
    "snow_melt": ("melt", "mm/h", "surface melt"),
    "snow_rain_plus_melt": ("rain_plus_melt", "mm/h", "water leaving the snowpack plus rain on bare ground"),
    "snow_rainfall": ("rainfall", "mm/h", "liquid precipitation"),
    "snow_snowfall": ("snowfall", "mm/h", "solid precipitation after the snowfall correction factor"),
    "snow_cover_frac": ("snow_cover_frac", "1", "snow-covered area fraction (areal depletion curve)"),
    "snow_liquid_water": ("liquid_water", "mm", "liquid water held or in transit in the pack"),
    "snow_cold_content": ("cold_content", "mm", "heat deficit as mm of water to refreeze"),
    "snow_depth": ("snow_depth", "m", "snow depth"),
    "snow_ros_melt": ("ros_melt", "mm/h", "melt during rain-on-snow intervals"),
    "snow_ros_frac": ("rain_on_snow", "1", "area fraction with rain-on-snow this hour"),
    "sw_terrain": ("sw_terrain", "W/m2", "terrain-corrected shortwave (open sky)"),
    "sw_clear_terrain": ("sw_clear_terrain", "W/m2", "clear-sky terrain-corrected shortwave"),
}
BAND_FEATURES = {"snow_swe": "swe", "snow_rain_plus_melt": "rain_plus_melt", "snow_cover_frac": "snow_cover_frac"}
EXTRA_FEATURES = {"snow_line_elev": ("m", "elevation where precipitation is half snow (wet-bulb profile), clipped to 0-3000 m")}

LSTM_FEATURES = (
    list(BASIN_FEATURES)
    + list(EXTRA_FEATURES)
    + [f"{name}_b{b}" for name in BAND_FEATURES for b in range(1, N_FEATURE_BANDS + 1)]
)


def _weighted(ds: xr.Dataset, var: str, weights: np.ndarray) -> np.ndarray:
    return (ds[var].to_numpy() * weights[None, :]).sum(axis=1)


def snow_line_elevation(result: SnowResult) -> np.ndarray:
    ds = result.hru
    p = result.params
    tw_mid = 0.5 * (p.tw_snow + p.tw_rain)
    z = ds["elevation"].to_numpy()
    tw = ds["wet_bulb"].to_numpy()
    lo, hi = int(np.argmin(z)), int(np.argmax(z))
    if z[hi] - z[lo] > 50.0:
        slope = (tw[:, hi] - tw[:, lo]) / (z[hi] - z[lo])
        slope = np.where(slope < -1e-4, slope, p.temp_lapse_c_per_km / 1000.0)
    else:
        slope = np.full(tw.shape[0], p.temp_lapse_c_per_km / 1000.0)
    line = z[lo] + (tw_mid - tw[:, lo]) / slope
    return np.clip(line, 0.0, 3000.0)


def basin_features(result: SnowResult, n_bands: int = N_FEATURE_BANDS) -> pd.DataFrame:
    """Hourly LSTM features for the whole HRU set, columns = LSTM_FEATURES (float32)."""
    ds = result.hru
    w = result.hrus.area_weights()
    cols = {name: _weighted(ds, var, w) for name, (var, _, _) in BASIN_FEATURES.items()}
    cols["snow_line_elev"] = snow_line_elevation(result)
    band = ds["band"].to_numpy()
    for name, var in BAND_FEATURES.items():
        values = ds[var].to_numpy()
        for b in range(1, n_bands + 1):
            sel = band == b
            if sel.any():
                wb = w[sel] / w[sel].sum()
                cols[f"{name}_b{b}"] = (values[:, sel] * wb[None, :]).sum(axis=1)
            else:
                cols[f"{name}_b{b}"] = np.full(ds.sizes["time"], np.nan)
    frame = pd.DataFrame(cols, index=pd.DatetimeIndex(ds["time"].to_numpy(), name="time"))
    return frame[[c for c in LSTM_FEATURES if c in frame]].astype(np.float32)


def _grouped_states(result: SnowResult, key: str) -> xr.Dataset:
    ds = result.hru
    area = ds["area_km2"]
    values = ds.drop_vars([c for c in ("band", "elevation", "area_km2", "subbasin", "band_id") if c != key])
    return (values * area).groupby(key).sum("hru") / area.groupby(key).sum("hru")


def subbasin_states(result: SnowResult) -> xr.Dataset:
    """Area-weighted sub-basin aggregates of every per-HRU output: dims (time, subbasin)."""
    return _grouped_states(result, "subbasin")


def band_states(result: SnowResult) -> xr.Dataset:
    """Area-weighted elevation-band aggregates (aspect classes combined): dims (time, band_id)."""
    return _grouped_states(result, "band_id")


def map_payload(
    result: SnowResult,
    variables: tuple[str, ...] = ("swe", "rain_plus_melt", "melt", "snow_cover_frac", "rain_on_snow"),
    start=None,
    end=None,
    level: str = "band",
    geometry: dict | None = None,
    decimals: int = 2,
) -> dict:
    """Compact JSON for animated choropleths: geometry once, then values[var][time][feature].

    `level="band"` uses the elevation-band polygons built with the HRUs; `level="subbasin"` needs a sub-basin
    FeatureCollection with a `subbasin_id` property in `geometry`.
    """
    if level == "band":
        ds = band_states(result)
        geometry = geometry or result.hrus.geometry
        key, dim = "band_id", "band_id"
    elif level == "subbasin":
        if geometry is None:
            raise ValueError("sub-basin geometry is required")
        ds = subbasin_states(result)
        key, dim = "subbasin_id", "subbasin"
    else:
        raise ValueError(f"unknown level {level!r}")
    order = [str(v) for v in ds[dim].to_numpy()]
    ds = ds.sel(time=slice(start, end))
    by_id = {f["properties"][key]: f for f in (geometry or {"features": []})["features"]}
    features = [by_id[i] for i in order if i in by_id]
    keep = [order.index(f["properties"][key]) for f in features]
    values = {v: np.round(ds[v].transpose("time", dim).to_numpy()[:, keep], decimals).tolist() for v in variables}
    return {
        "level": level,
        "times": [pd.Timestamp(t).isoformat() + "Z" for t in ds["time"].to_numpy()],
        "geometry": {"type": "FeatureCollection", "features": features},
        "values": values,
    }
