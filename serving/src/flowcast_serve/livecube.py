"""The virtual real-time cube (production-architecture.md §2.3): a small per-run Zarr store in the training cube's
schema, so `flowcast_model`'s streaming dataset and model code run on live data unchanged.

Layout (as `flowcast_pipeline.dataset.cube` writes it): dynamic `(basin, time)` hour-ending arrays, SNODAS bands as
`(basin, band, time)`, statics `(basin,)`, and forecast products `(basin, {p}_init, {p}_member, {p}_lead)` for GEFS
(`gefs_*`) and, for the water-temperature model, the flow forecast (`flowfc_qobs_mm_h`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

N_BANDS = 4
AORC = [f"aorc_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2", "spfh_2m_gkg")]


@dataclass
class Product:
    inits: pd.DatetimeIndex  # UTC
    leads_h: np.ndarray
    values: dict[str, np.ndarray]  # feature -> [init, member, lead]


@dataclass
class LiveBasin:
    basin: str
    dynamic: pd.DataFrame  # hourly UTC index; columns are cube features (bands as `<name>_band{k}`)
    statics: dict[str, float]
    products: dict[str, Product] = field(default_factory=dict)  # product prefix -> Product, e.g. "gefs"


def write(path: Path, live: LiveBasin) -> Path:
    """Write a one-basin cube at `path` (replaced if it exists)."""
    index = pd.DatetimeIndex(live.dynamic.index).tz_convert(None)
    basin = np.array([live.basin], dtype=str)
    data_vars: dict[str, tuple] = {}
    band_cols: dict[str, list[str]] = {}
    for col in live.dynamic.columns:
        stem, _, band = col.rpartition("_band")
        if stem and band.isdigit():
            band_cols.setdefault(stem, []).append(col)
            continue
        data_vars[col] = (("basin", "time"), live.dynamic[col].to_numpy(np.float32)[None, :])
    for stem, cols in band_cols.items():
        cols = sorted(cols, key=lambda c: int(c.rpartition("_band")[2]))
        data_vars[stem] = (("basin", "band", "time"), np.stack([live.dynamic[c].to_numpy(np.float32) for c in cols])[None])
    for name in AORC:
        data_vars.setdefault(name, (("basin", "time"), np.full((1, len(index)), np.nan, np.float32)))
    for name, value in live.statics.items():
        data_vars[name] = (("basin",), np.array([value], dtype=np.float64))
    coords: dict[str, np.ndarray] = {"basin": basin, "time": index.values, "band": np.arange(N_BANDS, dtype=np.int8)}
    for p, prod in live.products.items():
        coords[f"{p}_init"] = pd.DatetimeIndex(prod.inits).tz_convert(None).values
        coords[f"{p}_lead"] = np.asarray(prod.leads_h, dtype=np.float32)
        n_member = next(iter(prod.values.values())).shape[1]
        coords[f"{p}_member"] = np.arange(n_member, dtype=np.int8)
        for name, arr in prod.values.items():
            data_vars[name] = (("basin", f"{p}_init", f"{p}_member", f"{p}_lead"), np.asarray(arr, np.float32)[None])
    ds = xr.Dataset(data_vars, coords=coords, attrs={"flowcast_schema": "flowcast-training-cube", "store": "live", "time_convention": "hour-ending"})
    path = Path(path)
    if path.exists():
        for p in sorted(path.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
    ds.to_zarr(str(path), mode="w", consolidated=True)
    return path
