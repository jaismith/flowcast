"""Point air temperature for the air2stream baseline.

Uses the Open-Meteo historical weather API (ERA5/ERA5-Land reanalysis, CC BY 4.0, free for
non-commercial use). Reanalysis is not available in real time, so forecasts driven by it are
"perfect forcing" runs.
"""

import os
from pathlib import Path

import pandas as pd
import requests

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def era5_daily_air_temperature(lat: float, lon: float, start: str, end: str, timezone: str = "America/New_York") -> pd.DataFrame:
    """Daily max and mean 2 m air temperature (degC) indexed by local date."""
    env = os.environ.get("FLOWCAST_CACHE_DIR")
    cache = (Path(env) if env else Path.home() / ".cache" / "flowcast") / "forcing"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"era5_{lat:.4f}_{lon:.4f}_{start}_{end}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    resp = requests.get(
        OPEN_METEO_ARCHIVE,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "daily": "temperature_2m_max,temperature_2m_mean",
            "timezone": timezone,
        },
        timeout=300,
    )
    resp.raise_for_status()
    daily = resp.json()["daily"]
    df = pd.DataFrame({"tmax": daily["temperature_2m_max"], "tmean": daily["temperature_2m_mean"]}, index=pd.to_datetime(daily["time"]))
    df.index.name = "date"
    df.to_parquet(path)
    return df
