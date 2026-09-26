"""Area weights mapping grid cells to basins (and to elevation bands inside basins).

Each basin polygon is rasterised onto a supersampled copy of the source grid and block-summed, giving
the covered fraction of every cell. Weights are that fraction times the cell's relative area (cos(lat)
on geographic grids). Rows are *not* normalised: the extractor divides by the summed weight of valid
cells, so partial domain coverage and missing cells are handled in one place.
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp
from rasterio.features import rasterize
from rasterio.transform import Affine

from .grids import Grid


def coverage(geom, grid: Grid, supersample: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(flat cell index, covered fraction, iy) for cells touched by `geom` (already in the grid CRS)."""
    minx, miny, maxx, maxy = geom.bounds
    ix0, ix1 = grid.index_range(minx, maxx, "x")
    iy0, iy1 = grid.index_range(miny, maxy, "y")
    if ix1 <= ix0 or iy1 <= iy0:
        return np.array([], int), np.array([]), np.array([], int)
    s = supersample
    fdx, fdy = grid.dx / s, grid.dy / s
    x_edge = grid.x0 + grid.dx * (ix0 - 0.5)
    y_edge = grid.y0 + grid.dy * (iy0 - 0.5)
    transform = Affine(fdx, 0.0, x_edge, 0.0, fdy, y_edge)
    shape = ((iy1 - iy0) * s, (ix1 - ix0) * s)
    fine = rasterize([(geom, 1)], out_shape=shape, transform=transform, fill=0, dtype="uint8", all_touched=False)
    frac = fine.reshape(iy1 - iy0, s, ix1 - ix0, s).mean(axis=(1, 3))
    iy, ix = np.nonzero(frac)
    return (iy + iy0) * grid.nx + (ix + ix0), frac[iy, ix], iy + iy0


def basin_weights(polygons: gpd.GeoSeries, grid: Grid, supersample: int) -> sp.csr_matrix:
    """Sparse (n_basins x ny*nx) weights in `polygons` order."""
    geoms = polygons.to_crs(grid.crs)
    rows, cols, vals = [], [], []
    for r, geom in enumerate(geoms):
        idx, frac, iy = coverage(geom, grid, supersample)
        w = frac * (np.cos(np.radians(grid.y0 + grid.dy * iy)) if grid.geographic else 1.0)
        rows.append(np.full(len(idx), r))
        cols.append(idx)
        vals.append(w)
    return sp.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(len(geoms), grid.ny * grid.nx),
    )


def band_weights(basin_w: sp.csr_matrix, cell_elev: np.ndarray, n_bands: int) -> tuple[sp.csr_matrix, np.ndarray]:
    """Split each basin row into `n_bands` equal-area elevation bands (by cell mean elevation).

    Returns (n_basins*n_bands x cells) weights, row = basin*n_bands + band, and each band's weight share.
    """
    rows, cols, vals = [], [], []
    share = np.zeros((basin_w.shape[0], n_bands))
    for b in range(basin_w.shape[0]):
        lo, hi = basin_w.indptr[b], basin_w.indptr[b + 1]
        idx, w = basin_w.indices[lo:hi], basin_w.data[lo:hi]
        order = np.argsort(cell_elev[idx], kind="stable")
        cum = np.cumsum(w[order]) - w[order] / 2
        band = np.minimum((cum / w.sum() * n_bands).astype(int), n_bands - 1)
        for k in range(n_bands):
            sel = order[band == k]
            rows.append(np.full(len(sel), b * n_bands + k))
            cols.append(idx[sel])
            vals.append(w[sel])
            share[b, k] = w[sel].sum() / w.sum()
    m = sp.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(basin_w.shape[0] * n_bands, basin_w.shape[1]),
    )
    return m, share


def tile_blocks(w: sp.csr_matrix, grid: Grid) -> pd.DataFrame:
    """Tiles (source chunks) with non-zero weight, and how many units touch each."""
    coo = w.tocoo()
    iy, ix = np.divmod(coo.col, grid.nx)
    tiles = grid.tile_of(iy, ix)
    df = pd.DataFrame({"tile": tiles, "unit": coo.row})
    return df.groupby("tile")["unit"].nunique().rename("n_units").reset_index()
