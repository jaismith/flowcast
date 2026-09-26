"""Solar geometry (NOAA GML solar position equations), clear-sky irradiance and direct/diffuse partitioning."""

import numpy as np
import pandas as pd

SOLAR_CONSTANT = 1361.0


def solar_position(times_utc, lat_deg, lon_deg):
    """Solar elevation (deg), azimuth (deg clockwise from north) and Earth-Sun distance factor 1/r^2.

    `times_utc` is anything `pd.DatetimeIndex` accepts (naive values are treated as UTC); lat/lon broadcast against it.
    """
    t = pd.DatetimeIndex(times_utc)
    if t.tz is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    unix_s = t.as_unit("ns").asi8 / 1e9
    jd = unix_s / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    minutes = np.mod(unix_s, 86400.0) / 60.0

    l0 = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = np.radians(m)
    c = np.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + np.sin(2 * mr) * (0.019993 - 0.000101 * jc) + np.sin(3 * mr) * 0.000289
    true_long = l0 + c
    true_anom = np.radians(m + c)
    omega = np.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * np.sin(omega)
    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = np.radians(mean_obliq + 0.00256 * np.cos(omega))
    decl = np.arcsin(np.sin(obliq) * np.sin(np.radians(app_long)))
    y = np.tan(obliq / 2.0) ** 2
    l0r = np.radians(l0)
    eq_time = 4.0 * np.degrees(
        y * np.sin(2 * l0r) - 2 * ecc * np.sin(mr) + 4 * ecc * y * np.sin(mr) * np.cos(2 * l0r)
        - 0.5 * y * y * np.sin(4 * l0r) - 1.25 * ecc * ecc * np.sin(2 * mr)
    )  # fmt: skip
    r = 1.000001018 * (1 - ecc * ecc) / (1 + ecc * np.cos(true_anom))

    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.asarray(lon_deg, dtype=np.float64)
    tst = np.mod(minutes + eq_time + 4.0 * lon, 1440.0)
    ha = np.radians(tst / 4.0 - 180.0)
    cos_zen = np.clip(np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(ha), -1.0, 1.0)
    zen = np.arccos(cos_zen)
    sin_zen = np.maximum(np.sin(zen), 1e-9)
    cos_az = np.clip((np.sin(lat) * cos_zen - np.sin(decl)) / (np.cos(lat) * sin_zen), -1.0, 1.0)
    az0 = np.degrees(np.arccos(cos_az))
    azimuth = np.where(ha > 0, np.mod(az0 + 180.0, 360.0), np.mod(540.0 - az0, 360.0))
    elevation = 90.0 - np.degrees(zen)
    return elevation, azimuth, 1.0 / (r * r)


def interval_sun(times, lat_deg, lon_deg, step_hours: float = 1.0, n_sub: int = 6, label: str = "end"):
    """Sun positions at `n_sub` evenly spaced instants inside each interval: arrays shaped (time, n_sub).

    `label` says where the time stamp sits in the interval: "end", "start" or "center".
    """
    t = pd.DatetimeIndex(times)
    step = pd.Timedelta(hours=step_hours)
    offsets = (np.arange(n_sub) + 0.5) / n_sub
    start = {"end": t - step, "start": t, "center": t - step / 2}[label]
    samples = [start + step * f for f in offsets]
    flat = pd.DatetimeIndex(np.concatenate([s.values for s in samples]))
    el, az, e0 = solar_position(flat, lat_deg, lon_deg)
    shape = (n_sub, len(t))
    return el.reshape(shape).T, az.reshape(shape).T, e0.reshape(shape).T


def erbs_diffuse_fraction(kt):
    kt = np.asarray(kt, dtype=np.float64)
    mid = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    return np.where(kt <= 0.22, 1.0 - 0.09 * kt, np.where(kt <= 0.8, mid, 0.165))


def clear_sky_ghi(sin_el):
    """Haurwitz clear-sky global horizontal irradiance, W/m2."""
    s = np.maximum(np.asarray(sin_el, dtype=np.float64), 0.0)
    return np.where(s > 0.01, 1098.0 * s * np.exp(-0.057 / np.maximum(s, 0.01)), 0.0)
