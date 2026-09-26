"""Terrain radiation factors from a DEM: slope, aspect, horizon angles, sky-view factor and a per-HRU
direct-beam illumination table over sun positions.

The illumination table K[hru, azimuth, elevation] is the HRU-mean of max(cos i, 0) * [sun above local horizon],
i.e. the beam irradiance on the HRU per unit direct-normal irradiance. It is computed once per HRU set and applied
to any forcing at run time.
"""

import math

import numpy as np
from numba import njit, prange

EARTH_RADIUS_M = 6371000.0
LUT_AZIMUTHS = np.arange(0.0, 360.0, 5.0)
LUT_ELEVATIONS = np.arange(0.0, 90.1, 3.0)


def slope_aspect(elev: np.ndarray, pixel_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) slope (rad) and aspect (rad, clockwise from north, direction the slope faces).

    `pixel_m` is the pixel size per row (rows run north to south).
    """
    z = np.pad(elev.astype(np.float64), 1, mode="edge")
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    px = pixel_m[:, None]
    dz_east = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * px)
    dz_north = ((a + 2 * b + c) - (g + 2 * h + i)) / (8.0 * px)
    slope = np.arctan(np.hypot(dz_east, dz_north))
    aspect = np.mod(np.arctan2(-dz_east, -dz_north), 2 * np.pi)
    return slope, aspect


def _sample_distances(pixel_m: float, max_dist_m: float) -> np.ndarray:
    d = list(pixel_m * np.arange(1, 21))
    while d[-1] < max_dist_m:
        d.append(d[-1] * 1.08)
    return np.asarray(d)


@njit(parallel=True, cache=True)
def _horizons(elev, pixel_m, n_dir, dist_m):
    ny, nx = elev.shape
    out = np.zeros((n_dir, ny, nx), dtype=np.float32)
    nd = dist_m.shape[0]
    for i in prange(ny):
        scale = pixel_m[i]
        for k in range(n_dir):
            phi = 2.0 * math.pi * k / n_dir
            dr = -math.cos(phi)
            dc = math.sin(phi)
            for j in range(nx):
                z0 = elev[i, j]
                best = -1e9
                for s in range(nd):
                    dpx = dist_m[s] / scale
                    ii = int(round(i + dr * dpx))
                    jj = int(round(j + dc * dpx))
                    if ii < 0 or ii >= ny or jj < 0 or jj >= nx:
                        break
                    d = dist_m[s]
                    tan = (elev[ii, jj] - z0 - d * d / (2.0 * EARTH_RADIUS_M)) / d
                    if tan > best:
                        best = tan
                out[k, i, j] = math.atan(best) if best > -1e8 else 0.0
    return out


def horizon_angles(elev: np.ndarray, pixel_m: np.ndarray, n_dir: int = 16, max_dist_m: float = 10000.0) -> np.ndarray:
    """Horizon elevation angle (rad) in `n_dir` azimuths (clockwise from north): shape (n_dir, ny, nx)."""
    dist_m = _sample_distances(float(np.min(pixel_m)), max_dist_m)
    return _horizons(elev.astype(np.float64), pixel_m.astype(np.float64), n_dir, dist_m)


def sky_view_factor(horizons: np.ndarray, slope: np.ndarray, aspect: np.ndarray) -> np.ndarray:
    """Isotropic sky-view factor for a tilted surface (Dozier & Frew 1990), clipped to [0, 1].

    Horizon zenith angles are capped at 90 degrees; the surface's own tilt is handled by the formula.
    """
    n_dir = horizons.shape[0]
    phi = 2 * np.pi * np.arange(n_dir) / n_dir
    total = np.zeros(slope.shape)
    cos_s, sin_s = np.cos(slope), np.sin(slope)
    for k in range(n_dir):
        rel = np.cos(phi[k] - aspect)
        hz = np.pi / 2 - np.maximum(horizons[k], 0.0)
        total += cos_s * np.sin(hz) ** 2 + sin_s * rel * (hz - np.sin(hz) * np.cos(hz))
    return np.clip(total / n_dir, 0.0, 1.0)


@njit(parallel=True, cache=True)
def _illumination_lut(labels, n_hru, slope, aspect, hor, lut_az, lut_el):
    n_cells = labels.shape[0]
    n_dir = hor.shape[0]
    na = lut_az.shape[0]
    ne = lut_el.shape[0]
    sums = np.zeros((n_hru, na, ne))
    counts = np.zeros(n_hru)
    for c in range(n_cells):
        counts[labels[c]] += 1.0
    for a in prange(na):
        az = math.radians(lut_az[a])
        pos = lut_az[a] / (360.0 / n_dir)
        k0 = int(math.floor(pos)) % n_dir
        k1 = (k0 + 1) % n_dir
        w = pos - math.floor(pos)
        for c in range(n_cells):
            h_az = (1.0 - w) * hor[k0, c] + w * hor[k1, c]
            cs = math.cos(slope[c])
            ss = math.sin(slope[c])
            ca = math.cos(az - aspect[c])
            lab = labels[c]
            for e in range(ne):
                el = math.radians(lut_el[e])
                if el <= h_az:
                    continue
                cos_i = cs * math.sin(el) + ss * math.cos(el) * ca
                if cos_i > 0.0:
                    sums[lab, a, e] += cos_i
    for hh in range(n_hru):
        if counts[hh] > 0:
            sums[hh] /= counts[hh]
    return sums


def illumination_lut(labels: np.ndarray, n_hru: int, slope: np.ndarray, aspect: np.ndarray, horizons: np.ndarray) -> np.ndarray:
    """K[hru, az, el] from per-cell arrays (cells already selected; `horizons` is (n_dir, n_cells))."""
    return _illumination_lut(
        labels.astype(np.int64), n_hru, slope.astype(np.float64), aspect.astype(np.float64),
        horizons.astype(np.float64), LUT_AZIMUTHS, LUT_ELEVATIONS,
    )  # fmt: skip


def flat_lut(n_hru: int) -> np.ndarray:
    """Illumination table for horizontal, unobstructed HRUs: K = sin(elevation)."""
    k = np.sin(np.radians(LUT_ELEVATIONS))
    return np.broadcast_to(k, (n_hru, len(LUT_AZIMUTHS), len(LUT_ELEVATIONS))).copy()


def lookup(lut: np.ndarray, az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    """Bilinear lookup of K for sun positions (any shape) for every HRU: returns (..., hru)."""
    step_a = LUT_AZIMUTHS[1] - LUT_AZIMUTHS[0]
    step_e = LUT_ELEVATIONS[1] - LUT_ELEVATIONS[0]
    na, ne = len(LUT_AZIMUTHS), len(LUT_ELEVATIONS)
    fa = np.mod(np.asarray(az_deg), 360.0) / step_a
    a0 = np.floor(fa).astype(int) % na
    a1 = (a0 + 1) % na
    wa = fa - np.floor(fa)
    fe = np.clip(np.asarray(el_deg), 0.0, LUT_ELEVATIONS[-1]) / step_e
    e0 = np.minimum(np.floor(fe).astype(int), ne - 2)
    e1 = e0 + 1
    we = fe - e0
    L = np.moveaxis(lut, 0, -1)  # (az, el, hru)
    wa, we = wa[..., None], we[..., None]
    return (
        (1 - wa) * (1 - we) * L[a0, e0] + wa * (1 - we) * L[a1, e0]
        + (1 - wa) * we * L[a0, e1] + wa * we * L[a1, e1]
    )  # fmt: skip
