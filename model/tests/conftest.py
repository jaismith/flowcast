import numpy as np
import pandas as pd
import pytest

from flowcast_model.cube import write_cube

BASINS = ["01000001", "01000002", "01000003"]
FEATURES = ["precip", "temp", "qobs"]


def synthetic_frames(start="2017-10-01", end="2022-09-30T23:00", seed=0) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, end, freq="h")
    frames = {}
    for k, b in enumerate(BASINS):
        n = len(index)
        precip = rng.gamma(0.3, 1.5, n).astype(np.float32) * (rng.random(n) < 0.2)
        temp = (10 + 10 * np.sin(np.arange(n) * 2 * np.pi / 8760) + rng.normal(0, 2, n)).astype(np.float32)
        q = np.convolve(precip, np.exp(-np.arange(200) / 40.0), mode="full")[:n].astype(np.float32) * 0.02 * (k + 1) + 0.01
        df = pd.DataFrame({"precip": precip, "temp": temp, "qobs": q}, index=index)
        # gaps: a missing stretch of target and of one forcing
        df.iloc[12000 + 500 * k : 12400 + 500 * k, df.columns.get_loc("qobs")] = np.nan
        df.iloc[9000 : 9030 + 10 * k, df.columns.get_loc("temp")] = np.nan
        frames[b] = df
    return frames


@pytest.fixture
def cube_path(tmp_path):
    frames = synthetic_frames()
    static = pd.DataFrame({"area_km2": [120.0, 450.0, 900.0], "elev": [300.0, 520.0, 610.0], "lat": [41.9, 42.1, 42.4]}, index=BASINS)
    path = tmp_path / "cube.zarr"
    write_cube(path, frames, static, {"target_unit": "mm/h", "area_attribute": "area_km2"}, time_chunk=8760)
    return path
