"""Terrain-corrected shortwave per HRU from flat-surface global horizontal irradiance (GHI).

GHI is split into beam and diffuse with the Erbs clearness-index model. The beam part is scaled by the HRU's
illumination table (slope, aspect and horizon shading) averaged over sub-interval sun positions, the diffuse part by
the sky-view factor, and terrain-reflected light is added from the obstructed part of the sky.
"""

import numpy as np
import pandas as pd

from . import terrain
from .hru import HRUSet
from .solar import SOLAR_CONSTANT, clear_sky_ghi, erbs_diffuse_fraction, interval_sun

GROUND_ALBEDO = 0.3
MAX_BEAM_RATIO = 5.0
MISSING_GHI_CLEARNESS = 0.55


def terrain_shortwave(
    times: pd.DatetimeIndex,
    ghi: np.ndarray | None,
    hrus: HRUSet,
    step_hours: float = 1.0,
    label: str = "end",
    n_sub: int = 6,
) -> dict[str, np.ndarray]:
    """Returns `sw_terrain`, `sw_clear_terrain` (time, hru) in W/m2 and `ghi_clear` (time, hru).

    `ghi` is (time,) or (time, hru); None or NaN values fall back to a fixed fraction of clear-sky GHI.
    """
    nt, nh = len(times), hrus.n
    lat = hrus.table["lat"].to_numpy()
    lon = hrus.table["lon"].to_numpy()
    svf = hrus.table["svf"].to_numpy()[None, :]
    groups: dict[tuple[float, float], list[int]] = {}
    for h in range(nh):
        groups.setdefault((round(lat[h], 1), round(lon[h], 1)), []).append(h)

    rb = np.zeros((nt, nh))
    i0h = np.zeros((nt, nh))
    ghi_cs = np.zeros((nt, nh))
    for (glat, glon), idx in groups.items():
        el, az, e0 = interval_sun(times, glat, glon, step_hours=step_hours, n_sub=n_sub, label=label)
        sin_el = np.maximum(np.sin(np.radians(el)), 0.0)
        k = terrain.lookup(hrus.lut[idx], az, el)  # (time, n_sub, hru)
        k = np.where((el > 0)[..., None], k, 0.0)
        sum_sin = sin_el.sum(axis=1)
        ratio = np.where(sum_sin[:, None] > 1e-3, k.sum(axis=1) / np.maximum(sum_sin, 1e-3)[:, None], 0.0)
        rb[:, idx] = np.minimum(ratio, MAX_BEAM_RATIO)
        i0h[:, idx] = (SOLAR_CONSTANT * e0 * sin_el).mean(axis=1)[:, None]
        ghi_cs[:, idx] = clear_sky_ghi(sin_el).mean(axis=1)[:, None]

    if ghi is None:
        g = np.full((nt, nh), np.nan)
    else:
        g = np.asarray(ghi, dtype=np.float64)
        g = np.broadcast_to(g[:, None] if g.ndim == 1 else g, (nt, nh)).copy()
    missing = ~np.isfinite(g)
    g[missing] = MISSING_GHI_CLEARNESS * ghi_cs[missing]
    g = np.maximum(g, 0.0)

    def partition(glob):
        kt = np.where(i0h > 1.0, np.clip(glob / np.maximum(i0h, 1.0), 0.0, 1.0), 0.0)
        kd = np.where(i0h > 1.0, erbs_diffuse_fraction(kt), 1.0)
        diffuse = kd * glob
        beam = glob - diffuse
        return beam * rb + diffuse * svf + GROUND_ALBEDO * glob * (1.0 - svf)

    return {"sw_terrain": partition(g), "sw_clear_terrain": partition(ghi_cs), "ghi_clear": ghi_cs, "ghi": g}
