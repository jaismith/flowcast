"""Snow features as a training-dataset step: one call per basin.

    hrus = load_or_build_hrus("USGS-01423000", cache_dir)          # DEM, bands, terrain tables (cached)
    features = snow_features_for_basin(hrus, forcing)              # DataFrame[time, LSTM_FEATURES]

`forcing` is hourly and either basin-mean (DataFrame indexed by UTC time; AORC or canonical names) or per HRU
(xarray Dataset with dims (time, hru)). Start the forcing at the beginning of a water year (Oct 1) or earlier so the
model spins up from snow-free conditions; `spinup` drops leading rows from the returned features.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import shapely
import xarray as xr

from .features import LSTM_FEATURES, basin_features
from .hru import HRUSet, build_hrus, fetch_nldi_basin
from .model import AORC_NAMES, AORC_RADIATION_LABEL, forcing_from_aorc, run_snow
from .params import NORTHEAST, SnowParams


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


def snow_features_for_basin(
    hrus: HRUSet,
    forcing: pd.DataFrame | xr.Dataset,
    params: SnowParams = NORTHEAST,
    forcing_elevation: float | np.ndarray | None = None,
    spinup: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    radiation_label = None
    if any(name in forcing for name in AORC_NAMES):
        forcing = forcing_from_aorc(forcing)
        radiation_label = AORC_RADIATION_LABEL
    result = run_snow(hrus, forcing, params=params, forcing_elevation=forcing_elevation, radiation_label=radiation_label)
    feats = basin_features(result)
    if spinup is not None:
        feats = feats.loc[pd.Timestamp(spinup) :]
    return feats[LSTM_FEATURES]
