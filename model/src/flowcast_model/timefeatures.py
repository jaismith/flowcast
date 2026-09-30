"""Calendar inputs for the flow model: day of year, local clock hour and day of week, as sin/cos pairs.

Known for any time, so they can feed both the hindcast and the forecast branch. Values are hour-ending (each covers
the hour before), so the mid-hour is used. Clock hour and weekday are civil local time with daylight saving: water
releases (hydropeaking) follow the clock and the working week, not the sun.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .tempcube import time_harmonics

CALENDAR_FEATURES = ("cal_doy_sin", "cal_doy_cos", "cal_hour_sin", "cal_hour_cos", "cal_dow_sin", "cal_dow_cos")
EASTERN, CENTRAL = "America/New_York", "America/Chicago"


def basin_timezone(lon: float | None) -> str:
    """Eastern or Central time. The study area spans both; the boundary runs near 86 W through Indiana, Kentucky and
    Tennessee, so basins right at it may be off by an hour. Without a longitude, Eastern."""
    return CENTRAL if lon is not None and np.isfinite(lon) and lon < -86.0 else EASTERN


def calendar_features(times: pd.DatetimeIndex, lon: float | None) -> dict[str, np.ndarray]:
    """{feature: values} for naive-UTC hour-ending `times` at a basin with longitude `lon`."""
    doy = time_harmonics(times, np.array([0.0 if lon is None else lon]))
    local = (times - pd.Timedelta(minutes=30)).tz_localize("UTC").tz_convert(basin_timezone(lon))
    day = (local.hour.to_numpy() + local.minute.to_numpy() / 60.0) / 24.0
    week = (local.dayofweek.to_numpy() + day) / 7.0
    return {
        "cal_doy_sin": doy["doy_sin"][0],
        "cal_doy_cos": doy["doy_cos"][0],
        "cal_hour_sin": np.sin(2 * np.pi * day).astype(np.float32),
        "cal_hour_cos": np.cos(2 * np.pi * day).astype(np.float32),
        "cal_dow_sin": np.sin(2 * np.pi * week).astype(np.float32),
        "cal_dow_cos": np.cos(2 * np.pi * week).astype(np.float32),
    }
