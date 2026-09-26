"""Snow features as a training-dataset step: one call per basin.

    hrus = load_or_build_hrus("USGS-01423000", cache_dir, geometry=camelsh_polygon)   # DEM, bands, terrain (cached)
    forcing, z = forcing_from_cube(cube.sel(basin="01423000"), hrus)                   # training cube v1 -> per-HRU
    features = snow_features_for_basin(hrus, forcing, forcing_elevation=z, radiation_label="center")

`forcing` is hourly and either basin-mean (DataFrame indexed by UTC time; AORC, cube or canonical names) or per HRU
(xarray Dataset with dims (time, hru)). Start the forcing at the beginning of a water year (Oct 1) or earlier so the
model spins up from snow-free conditions; `spinup` drops leading rows from the returned features.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import shapely
import xarray as xr

from . import meteo
from .features import LSTM_FEATURES, basin_features
from .hru import HRUSet, build_hrus, fetch_nldi_basin
from .model import AORC_NAMES, AORC_RADIATION_LABEL, forcing_from_aorc, run_snow
from .params import NORTHEAST, SnowParams

# Training cube v1 variable vocabulary (flowcast_pipeline.dataset.sources.OUTPUTS) -> canonical names.
CUBE_NAMES = {
    "precip_mm_h": "precip",
    "temp_2m_c": "air_temperature",
    "dewpoint_2m_c": "dewpoint_temperature",
    "wind_speed_10m": "wind_speed",
    "sw_down_wm2": "shortwave_down",
}


def load_or_build_hrus(
    basin_id: str,
    cache_dir: Path | str,
    geometry: shapely.Geometry | None = None,
    n_bands: int = 4,
    forest_frac: float | str = "worldcover",
) -> HRUSet:
    """HRUs for a gauge basin (one sub-basin, `n_bands` equal-area elevation bands), cached under cache_dir/basin_id."""
    path = Path(cache_dir) / basin_id
    if (path / "hrus.parquet").exists():
        return HRUSet.load(path)
    geometry = geometry if geometry is not None else fetch_nldi_basin(basin_id)
    hrus = build_hrus({basin_id: geometry}, n_bands=n_bands, forest_frac=forest_frac)
    hrus.save(path)
    return hrus


def forcing_from_cube(basin: xr.Dataset, hrus: HRUSet, source: str = "aorc") -> tuple[xr.Dataset, np.ndarray]:
    """Per-HRU canonical forcing from one basin of the training cube, plus the elevation each HRU's forcing represents.

    Band variables (`{source}_band_<var>`, dims (band, time), band 0 = lowest) go to the HRUs of the matching
    equal-area band; basin-only variables (`{source}_<var>`, e.g. wind and pressure) go to every HRU, with pressure
    moved hypsometrically from the basin to the band elevation (`band_elev_m`).
    """
    band_of_hru = hrus.table["band"].to_numpy() - 1
    band_elev = basin["band_elev_m"].to_numpy().astype(np.float64)
    times = pd.DatetimeIndex(basin["time"].to_numpy())
    data = {}
    for cube_name, name in CUBE_NAMES.items():
        band_var, basin_var = f"{source}_band_{cube_name}", f"{source}_{cube_name}"
        if band_var in basin:
            values = basin[band_var].transpose("band", "time").to_numpy().astype(np.float64)[band_of_hru].T
        elif basin_var in basin:
            values = np.repeat(basin[basin_var].to_numpy().astype(np.float64)[:, None], hrus.n, axis=1)
        else:
            continue
        data[name] = (("time", "hru"), values)
    pressure = f"{source}_pressure_kpa"
    if pressure in basin:
        w = basin["band_area_frac"].to_numpy() if "band_area_frac" in basin else np.full(len(band_elev), 1 / len(band_elev))
        z_basin = float(np.sum(w * band_elev) / np.sum(w))
        t_basin = basin[f"{source}_temp_2m_c"].to_numpy() if f"{source}_temp_2m_c" in basin else np.zeros(len(times))
        p_pa = basin[pressure].to_numpy().astype(np.float64) * 1000.0
        dz = band_elev[band_of_hru][None, :] - z_basin
        data["surface_pressure"] = (("time", "hru"), meteo.pressure_at_elevation(p_pa[:, None], t_basin[:, None], dz))
    ds = xr.Dataset(data, coords={"time": times, "hru": hrus.ids})
    return ds, band_elev[band_of_hru]


def snow_features_for_basin(
    hrus: HRUSet,
    forcing: pd.DataFrame | xr.Dataset,
    params: SnowParams = NORTHEAST,
    forcing_elevation: float | np.ndarray | None = None,
    spinup: str | pd.Timestamp | None = None,
    radiation_label: str | None = None,
) -> pd.DataFrame:
    if any(name in forcing for name in AORC_NAMES):
        forcing = forcing_from_aorc(forcing)
        radiation_label = radiation_label or AORC_RADIATION_LABEL
    elif any(name in forcing for name in CUBE_NAMES):
        rename = {k: v for k, v in CUBE_NAMES.items() if k in forcing}
        forcing = forcing.rename(columns=rename) if isinstance(forcing, pd.DataFrame) else forcing.rename(rename)
        if "pressure_kpa" in forcing:
            forcing["surface_pressure"] = forcing["pressure_kpa"] * 1000.0
    result = run_snow(hrus, forcing, params=params, forcing_elevation=forcing_elevation, radiation_label=radiation_label)
    feats = basin_features(result)
    if spinup is not None:
        feats = feats.loc[pd.Timestamp(spinup) :]
    return feats[LSTM_FEATURES]
