"""Terrain inputs for the snow and radiation module, on the AORC 1 km grid.

From the Copernicus GLO-90 DEM (3 arcsec, public on AWS), per 90 m pixel:

* elevation, slope, and aspect as northness/eastness;
* an isotropic sky-view factor, (1 + cos slope) / 2;
* a clear-sky *terrain shortwave factor* per month: daily-integrated top-of-atmosphere beam irradiance on
  the sloped pixel divided by that on a horizontal surface (mid-month day, 30 min steps). Horizon
  shading by surrounding terrain is not included in v1.

These are averaged onto AORC cells; basin and elevation-band values are weight-averages of the cells.
"""

import logging
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.transform import Affine

from .grids import AORC

log = logging.getLogger(__name__)

DEM_URL = "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com/Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
LAYERS = ["elev_m", "slope_deg", "northness", "eastness", "sky_view", *[f"sw_factor_m{m:02d}" for m in range(1, 13)]]
MID_MONTH_DOY = np.array([15, 46, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349])


def dem_url(lat: int, lon: int) -> str:
    return DEM_URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat), ew="W" if lon < 0 else "E", lon=abs(lon))


def sw_factors(slope: np.ndarray, aspect: np.ndarray, lat_deg: np.ndarray) -> np.ndarray:
    """(12, ...) clear-sky beam ratio sloped/horizontal. `aspect` is clockwise from north, radians."""
    phi = np.radians(lat_deg)
    out = np.empty((12, *slope.shape), dtype=np.float32)
    omegas = np.radians(np.arange(-180, 180, 7.5))
    for m, doy in enumerate(MID_MONTH_DOY):
        decl = np.radians(23.45) * np.sin(2 * np.pi * (284 + doy) / 365)
        on_slope = np.zeros(slope.shape)
        flat = np.zeros(slope.shape)
        for w in omegas:
            cos_z = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(w)
            up = cos_z > 0
            sin_z = np.sqrt(np.clip(1 - cos_z**2, 0, 1))
            # Solar azimuth clockwise from north.
            cos_az = np.where(sin_z > 1e-6, (np.sin(decl) - np.sin(phi) * cos_z) / (np.cos(phi) * sin_z + 1e-12), 1.0)
            az = np.arccos(np.clip(cos_az, -1, 1))
            az = np.where(w > 0, 2 * np.pi - az, az)
            cos_i = cos_z * np.cos(slope) + sin_z * np.sin(slope) * np.cos(az - aspect)
            on_slope += np.where(up, np.clip(cos_i, 0, None), 0)
            flat += np.where(up, cos_z, 0)
        out[m] = on_slope / np.maximum(flat, 1e-9)
    return out


def tile_layers(lat: int, lon: int) -> tuple[np.ndarray, Affine] | None:
    """All LAYERS at 90 m for one 1x1 degree DEM tile, or None if the tile doesn't exist (ocean)."""
    try:
        with rasterio.open(dem_url(lat, lon)) as src:
            z = src.read(1).astype(np.float64)
            transform = src.transform
    except rasterio.errors.RasterioIOError:
        return None
    ny, nx = z.shape
    lats = transform.f + transform.e * (np.arange(ny) + 0.5)
    dy = abs(transform.e) * 110_574.0
    dx = transform.a * 111_320.0 * np.cos(np.radians(lats))[:, None]
    gy, gx = np.gradient(z)
    dzdy = -gy / dy  # rows run north to south
    dzdx = gx / dx
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.mod(np.arctan2(-dzdx, -dzdy), 2 * np.pi)  # downslope direction, clockwise from north
    lat_grid = np.broadcast_to(lats[:, None], z.shape)
    stack = np.concatenate(
        [
            np.stack([z, np.degrees(slope), np.cos(aspect) * np.sin(slope), np.sin(aspect) * np.sin(slope), (1 + np.cos(slope)) / 2]),
            sw_factors(slope, aspect, lat_grid),
        ]
    ).astype(np.float32)
    return stack, transform


def _aorc_window(lat: int, lon: int) -> tuple[int, int, int, int]:
    iy0, iy1 = AORC.index_range(lat, lat + 1, "y")
    ix0, ix1 = AORC.index_range(lon, lon + 1, "x")
    return iy0, iy1, ix0, ix1


def _process(args: tuple[int, int, Path]) -> Path | None:
    lat, lon, out_dir = args
    path = out_dir / f"terrain_{lat}_{lon}.npz"
    if path.exists():
        return path
    res = tile_layers(lat, lon)
    if res is None:
        return None
    stack, src_transform = res
    iy0, iy1, ix0, ix1 = _aorc_window(lat, lon)
    # AORC rows ascend in latitude; reproject onto a north-up window, then flip.
    dst_transform = Affine(AORC.dx, 0, AORC.x0 + AORC.dx * (ix0 - 0.5), 0, -AORC.dy, AORC.y0 + AORC.dy * (iy1 - 0.5))
    dst = np.full((len(LAYERS), iy1 - iy0, ix1 - ix0), np.nan, dtype=np.float32)
    for k in range(len(LAYERS)):
        reproject(
            stack[k], dst[k], src_transform=src_transform, src_crs="EPSG:4326", dst_transform=dst_transform,
            dst_crs="EPSG:4326", resampling=Resampling.average, src_nodata=np.nan, dst_nodata=np.nan,
        )
    np.savez_compressed(path, layers=dst[:, ::-1, :], window=np.array([iy0, iy1, ix0, ix1]))
    return path


def build(degree_tiles: list[tuple[int, int]], out_dir: Path, workers: int = 8) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(workers) as pool:
        paths = list(pool.map(_process, [(lat, lon, out_dir) for lat, lon in degree_tiles]))
    return [p for p in paths if p is not None]


def assemble(paths: list[Path], cells: np.ndarray) -> np.ndarray:
    """(len(LAYERS), len(cells)) values for flat AORC cell indices `cells`."""
    iy, ix = np.divmod(cells, AORC.nx)
    out = np.full((len(LAYERS), len(cells)), np.nan, dtype=np.float32)
    for path in paths:
        with np.load(path) as f:
            layers, (iy0, iy1, ix0, ix1) = f["layers"], f["window"]
        m = (iy >= iy0) & (iy < iy1) & (ix >= ix0) & (ix < ix1)
        out[:, m] = layers[:, iy[m] - iy0, ix[m] - ix0]
    return out
