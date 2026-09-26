"""Source grids: coordinates, CRS and chunk layout for every gridded forcing product.

Axis coordinates are cell centres on a regular spacing (which may be negative, i.e. descending).
"""

from dataclasses import dataclass
from functools import cached_property

import numpy as np
from pyproj import CRS

GEOGRAPHIC = CRS.from_epsg(4326)


@dataclass(frozen=True)
class Grid:
    name: str
    crs_wkt: str
    x0: float  # first x centre
    dx: float
    nx: int
    y0: float  # first y centre
    dy: float
    ny: int
    chunk_y: int
    chunk_x: int

    @cached_property
    def crs(self) -> CRS:
        return CRS.from_wkt(self.crs_wkt)

    @property
    def geographic(self) -> bool:
        return self.crs.is_geographic

    @property
    def x(self) -> np.ndarray:
        return self.x0 + self.dx * np.arange(self.nx)

    @property
    def y(self) -> np.ndarray:
        return self.y0 + self.dy * np.arange(self.ny)

    def index_range(self, lo: float, hi: float, axis: str) -> tuple[int, int]:
        """[start, stop) of cells whose extent intersects [lo, hi] along `axis`."""
        c0, d, n = (self.x0, self.dx, self.nx) if axis == "x" else (self.y0, self.dy, self.ny)
        a, b = sorted(((lo - c0) / d, (hi - c0) / d))
        start = int(np.floor(a + 0.5))
        stop = int(np.floor(b + 0.5)) + 1
        return max(start, 0), min(stop, n)

    def tile_of(self, iy: np.ndarray, ix: np.ndarray) -> np.ndarray:
        return (iy // self.chunk_y) * self.tiles_x + ix // self.chunk_x

    @property
    def tiles_x(self) -> int:
        return -(-self.nx // self.chunk_x)


AORC = Grid("aorc", GEOGRAPHIC.to_wkt(), -130.0, 1 / 120, 8401, 20.0, 1 / 120, 4201, 128, 256)
MRMS = Grid("mrms", GEOGRAPHIC.to_wkt(), -129.995, 0.01, 7000, 54.995, -0.01, 3500, 100, 100)
GEFS = Grid("gefs", GEOGRAPHIC.to_wkt(), -180.0, 0.25, 1440, 90.0, -0.25, 721, 17, 16)


def hrrr(name: str, crs_wkt: str, x: np.ndarray, y: np.ndarray, chunk_y: int, chunk_x: int) -> Grid:
    return Grid(name, crs_wkt, float(x[0]), float(x[1] - x[0]), len(x), float(y[0]), float(y[1] - y[0]), len(y), chunk_y, chunk_x)
