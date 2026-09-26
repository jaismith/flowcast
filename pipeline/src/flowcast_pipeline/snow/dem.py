"""DEM access from AWS Terrain Tiles (Terrarium PNG, Web Mercator; 3DEP-derived over the US).

Web Mercator is conformal, so slopes, aspects and horizon directions can be computed directly on the tile grid
with a latitude-dependent pixel size; no reprojection is needed.
"""

import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
EARTH_CIRCUMFERENCE_M = 40075016.686
TILE = 256


def _cache_dir() -> Path:
    return Path(os.environ.get("FLOWCAST_CACHE_DIR", Path.home() / ".cache" / "flowcast")) / "terrain"


def lonlat_to_global_pixel(lon, lat, zoom: int):
    n = TILE * 2**zoom
    lat_r = np.radians(np.asarray(lat, dtype=np.float64))
    px = (np.asarray(lon, dtype=np.float64) + 180.0) / 360.0 * n
    py = (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / math.pi) / 2.0 * n
    return px, py


def global_pixel_to_lonlat(px, py, zoom: int):
    n = TILE * 2**zoom
    lon = np.asarray(px, dtype=np.float64) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * np.asarray(py, dtype=np.float64) / n))))
    return lon, lat


@dataclass
class DEMGrid:
    """Elevation (m) on a Web Mercator pixel grid; (px0, py0) is the global pixel of the top-left corner."""

    elev: np.ndarray
    zoom: int
    px0: int
    py0: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.elev.shape

    def lon(self) -> np.ndarray:
        return global_pixel_to_lonlat(self.px0 + np.arange(self.shape[1]) + 0.5, 0.0, self.zoom)[0]

    def lat(self) -> np.ndarray:
        return global_pixel_to_lonlat(0.0, self.py0 + np.arange(self.shape[0]) + 0.5, self.zoom)[1]

    def pixel_size_m(self) -> np.ndarray:
        """Ground size of a pixel for each row (m)."""
        return EARTH_CIRCUMFERENCE_M * np.cos(np.radians(self.lat())) / (TILE * 2**self.zoom)

    def sample(self, lon, lat) -> np.ndarray:
        """Nearest-pixel elevation at lon/lat points (NaN outside the grid)."""
        px, py = lonlat_to_global_pixel(lon, lat, self.zoom)
        i = np.floor(py - self.py0).astype(int)
        j = np.floor(px - self.px0).astype(int)
        ok = (i >= 0) & (i < self.shape[0]) & (j >= 0) & (j < self.shape[1])
        out = np.full(np.shape(i), np.nan)
        out[ok] = self.elev[i[ok], j[ok]]
        return out

    def pixel_bounds(self):
        """Global-pixel edges in x and y (for rasterizing geometries)."""
        return self.px0, self.py0, self.px0 + self.shape[1], self.py0 + self.shape[0]


def _fetch_tile(z: int, x: int, y: int, session: requests.Session) -> np.ndarray:
    path = _cache_dir() / str(z) / str(x) / f"{y}.png"
    if path.exists():
        data = path.read_bytes()
    else:
        resp = session.get(TILE_URL.format(z=z, x=x, y=y), timeout=60)
        resp.raise_for_status()
        data = resp.content
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def choose_zoom(bbox: tuple[float, float, float, float], max_pixels: float = 6e6, max_zoom: int = 12) -> int:
    lon0, lat0, lon1, lat1 = bbox
    for z in range(max_zoom, 5, -1):
        px0, py1 = lonlat_to_global_pixel(lon0, lat0, z)
        px1, py0 = lonlat_to_global_pixel(lon1, lat1, z)
        if (px1 - px0) * (py1 - py0) <= max_pixels:
            return z
    return 6


def fetch_dem(bbox: tuple[float, float, float, float], zoom: int | None = None, workers: int = 16) -> DEMGrid:
    """Mosaic Terrarium tiles covering `bbox` = (lon_min, lat_min, lon_max, lat_max), cropped to the bbox."""
    zoom = choose_zoom(bbox) if zoom is None else zoom
    lon0, lat0, lon1, lat1 = bbox
    gx0, gy1 = lonlat_to_global_pixel(lon0, lat0, zoom)
    gx1, gy0 = lonlat_to_global_pixel(lon1, lat1, zoom)
    tx0, tx1 = int(gx0 // TILE), int(gx1 // TILE)
    ty0, ty1 = int(gy0 // TILE), int(gy1 // TILE)
    mosaic = np.zeros(((ty1 - ty0 + 1) * TILE, (tx1 - tx0 + 1) * TILE), dtype=np.float32)
    session = requests.Session()
    jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
    with ThreadPoolExecutor(workers) as pool:
        tiles = pool.map(lambda xy: _fetch_tile(zoom, xy[0], xy[1], session), jobs)
        for (x, y), tile in zip(jobs, tiles):
            mosaic[(y - ty0) * TILE : (y - ty0 + 1) * TILE, (x - tx0) * TILE : (x - tx0 + 1) * TILE] = tile
    cx0, cy0 = int(gx0) - tx0 * TILE, int(gy0) - ty0 * TILE
    cx1, cy1 = int(math.ceil(gx1)) - tx0 * TILE, int(math.ceil(gy1)) - ty0 * TILE
    elev = mosaic[cy0:cy1, cx0:cx1]
    return DEMGrid(np.ascontiguousarray(elev), zoom, int(gx0), int(gy0))
