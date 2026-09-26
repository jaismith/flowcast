"""Tree cover on the DEM grid from ESA WorldCover 2021 (10 m, global, public COGs; CC-BY 4.0)."""

import math

import numpy as np
import rasterio
from rasterio.windows import from_bounds

from .dem import DEMGrid

WORLDCOVER_URL = "/vsicurl/https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
TREE_COVER = 10


def _tile_name(lat0: int, lon0: int) -> str:
    return f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"


def tree_cover(dem: DEMGrid, decimation: int = 2) -> np.ndarray:
    """Tree-cover indicator (0/1, float32) at each DEM pixel center, read from WorldCover overviews."""
    lon = dem.lon()
    lat = dem.lat()
    out = np.zeros(dem.shape, dtype=np.float32)
    lon_edges = range(int(math.floor(lon.min() / 3) * 3), int(math.floor(lon.max() / 3) * 3) + 1, 3)
    lat_edges = range(int(math.floor(lat.min() / 3) * 3), int(math.floor(lat.max() / 3) * 3) + 1, 3)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        for lat0 in lat_edges:
            for lon0 in lon_edges:
                rows = np.where((lat >= lat0) & (lat < lat0 + 3))[0]
                cols = np.where((lon >= lon0) & (lon < lon0 + 3))[0]
                if len(rows) == 0 or len(cols) == 0:
                    continue
                with rasterio.open(WORLDCOVER_URL.format(tile=_tile_name(lat0, lon0))) as src:
                    bounds = (lon[cols].min(), lat[rows].min(), lon[cols].max(), lat[rows].max())
                    window = from_bounds(*bounds, src.transform).round_offsets().round_lengths()
                    shape = (max(1, int(window.height) // decimation), max(1, int(window.width) // decimation))
                    data = src.read(1, window=window, out_shape=shape)
                    wt = src.window_transform(window)
                    sx = window.width / shape[1]
                    sy = window.height / shape[0]
                    j = np.clip(((lon[cols] - wt.c) / (wt.a * sx)).astype(int), 0, shape[1] - 1)
                    i = np.clip(((lat[rows] - wt.f) / (wt.e * sy)).astype(int), 0, shape[0] - 1)
                    out[np.ix_(rows, cols)] = (data[np.ix_(i, j)] == TREE_COVER).astype(np.float32)
    return out
