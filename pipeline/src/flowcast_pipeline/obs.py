"""Observation helpers: hourly series from instantaneous values and the `obs/` Parquet layout.

Layout: `<lake>/obs/<site_id>/<variable>/<YYYY-MM>.parquet` (see `lake.Lake`) with columns
`time` (UTC), `value`, `approval_status`, `qualifier`. Re-pulling a window replaces overlapping rows,
which is how USGS revisions to provisional data get picked up.
"""

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .lake import Lake
from .usgs.client import WaterDataClient
from .usgs.params import Parameter, site_id

VARIABLE_PARAMETERS: dict[str, Parameter] = {
    "discharge": Parameter.DISCHARGE,
    "water_temperature": Parameter.WATER_TEMPERATURE,
    "stage": Parameter.GAGE_HEIGHT,
}


def to_hourly(iv: pd.DataFrame, tolerance: str = "15min") -> pd.DataFrame:
    """Instantaneous value at the top of each UTC hour (nearest reading within `tolerance`)."""
    if iv.empty:
        return iv.iloc[0:0][["time", "value", "approval_status", "qualifier"]]
    df = iv.drop_duplicates("time").set_index("time").sort_index()
    hours = pd.date_range(df.index[0].ceil("h"), df.index[-1].floor("h"), freq="h")
    out = df[["value", "approval_status", "qualifier"]].reindex(hours, method="nearest", tolerance=pd.Timedelta(tolerance))
    out.index.name = "time"
    return out.reset_index()


def hourly_observations(
    client: WaterDataClient,
    site: str,
    variable: str,
    start: datetime | date | str,
    end: datetime | date | str,
    use_cache: bool = True,
) -> pd.Series:
    iv = client.continuous(site, VARIABLE_PARAMETERS[variable], start, end, use_cache=use_cache)
    hourly = to_hourly(iv)
    return hourly.set_index("time")["value"].rename(variable)


def obs_key(site: str, variable: str, month: str) -> str:
    return f"obs/{site_id(site)}/{variable}/{month}.parquet"


def write_obs(root: Lake | Path | str, site: str, variable: str, frame: pd.DataFrame) -> list[str]:
    """Merge `frame` (time, value, approval_status, qualifier) into monthly Parquet files; returns the keys written."""
    lake = root if isinstance(root, Lake) else Lake(root)
    written = []
    frame = frame.dropna(subset=["value"])
    for month, part in frame.groupby(frame["time"].dt.strftime("%Y-%m")):
        key = obs_key(site, variable, month)
        old = lake.read_parquet(key)
        if old is not None:
            part = pd.concat([old[~old["time"].isin(part["time"])], part], ignore_index=True)
        lake.write_parquet(key, part.sort_values("time").reset_index(drop=True))
        written.append(key)
    return written


def read_obs(root: Lake | Path | str, site: str, variable: str, start: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """All stored hourly rows for one site and variable (optionally from `start`), sorted by time."""
    lake = root if isinstance(root, Lake) else Lake(root)
    keys = lake.list(f"obs/{site_id(site)}/{variable}/")
    if start is not None:
        first = pd.Timestamp(start).strftime("%Y-%m")
        keys = [k for k in keys if k.rsplit("/", 1)[-1][:7] >= first]
    frames = [lake.read_parquet(k) for k in keys if k.endswith(".parquet")]
    if not frames:
        return pd.DataFrame(columns=["time", "value", "approval_status", "qualifier"])
    return pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)


def obs_series(root: Lake | Path | str, site: str, variable: str, start: str | pd.Timestamp | None = None) -> pd.Series:
    """Hourly observations from the lake as a UTC-indexed series (the harness's observation input)."""
    obs = read_obs(root, site, variable, start)
    return pd.Series(obs["value"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(obs["time"], utc=True)), name=variable)
