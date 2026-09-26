"""Vectorized near-surface meteorology: humidity conversions, wet-bulb temperature, rain/snow split, lapse rates."""

import numpy as np

from .params import PrecipSplit, SnowParams

G = 9.80665
RD = 287.05
EPS = 0.622


def esat_mb(t_c):
    """Saturation vapor pressure over water (Bolton 1980), mb."""
    t_c = np.asarray(t_c, dtype=np.float64)
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def vapor_pressure_from_specific_humidity(q, p_pa):
    """Vapor pressure (mb) from specific humidity (kg/kg) and pressure (Pa)."""
    q = np.asarray(q, dtype=np.float64)
    return q * (np.asarray(p_pa) / 100.0) / (EPS + (1.0 - EPS) * q)


def dewpoint_from_vapor_pressure(e_mb):
    x = np.log(np.maximum(np.asarray(e_mb, dtype=np.float64), 1e-3) / 6.112)
    return 243.5 * x / (17.67 - x)


def vapor_pressure_from_dewpoint(td_c):
    return esat_mb(td_c)


def wet_bulb(t_c, e_mb, p_pa, iterations: int = 6):
    """Psychrometric wet-bulb temperature (degC) by Newton iteration on e = esat(Tw) - gamma * p * (T - Tw)."""
    t = np.asarray(t_c, dtype=np.float64)
    e = np.minimum(np.asarray(e_mb, dtype=np.float64), esat_mb(t))
    p_mb = np.asarray(p_pa, dtype=np.float64) / 100.0
    gamma = 0.00066 * p_mb
    tw = t - (t - dewpoint_from_vapor_pressure(e)) / 3.0
    for _ in range(iterations):
        es = esat_mb(tw)
        f = es - gamma * (t - tw) - e
        dfdt = es * 17.67 * 243.5 / (tw + 243.5) ** 2 + gamma
        tw = tw - f / dfdt
    return np.minimum(tw, t)


def snow_fraction(t_c, tw_c, params: SnowParams):
    """Fraction of precipitation falling as snow."""
    if params.precip_split == PrecipSplit.AIR_TEMPERATURE:
        return (np.asarray(t_c) <= params.pxtemp).astype(np.float64)
    width = max(params.tw_rain - params.tw_snow, 1e-6)
    return np.clip((params.tw_rain - np.asarray(tw_c)) / width, 0.0, 1.0)


def pressure_at_elevation(p_ref_pa, t_ref_c, dz_m):
    """Hypsometric pressure adjustment over a height difference dz (m)."""
    return np.asarray(p_ref_pa) * np.exp(-G * np.asarray(dz_m) / (RD * (np.asarray(t_ref_c) + 273.15)))


def standard_pressure_pa(elev_m):
    """Pressure from elevation alone (Anderson 2006 SNOW-17 formula), Pa."""
    e = np.asarray(elev_m, dtype=np.float64) / 100.0
    return 100.0 * 33.86 * (29.9 - 0.335 * e + 0.00022 * np.power(np.maximum(e, 0.0), 2.4))


def wind_speed(u, v):
    return np.hypot(np.asarray(u, dtype=np.float64), np.asarray(v, dtype=np.float64))
