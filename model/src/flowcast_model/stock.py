"""Export a cube to NeuralHydrology's stock `generic` dataset layout, for the loader acceptance test (plan §5.2).

`<out>/time_series/<basin>.nc` (hourly, `date` index) and `<out>/attributes/attributes.csv`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .cube import Cube


def export_generic(cube: Cube, out: str | Path, basins: list[str], features: list[str], attributes: list[str], start: str, end: str) -> Path:
    out = Path(out)
    (out / "time_series").mkdir(parents=True, exist_ok=True)
    (out / "attributes").mkdir(parents=True, exist_ok=True)
    for basin in basins:
        df = cube.load_dynamic(basin, features, pd.Timestamp(start), pd.Timestamp(end))
        df.index.name = "date"
        df.to_xarray().to_netcdf(out / "time_series" / f"{basin}.nc")
    static = cube.load_static(basins, attributes)
    static.index.name = "gauge_id"
    static.to_csv(out / "attributes" / "attributes.csv")
    return out
