"""Observation helpers: hourly series from instantaneous values and the `obs/` Parquet layout.

Layout: `<root>/obs/<site_id>/<variable>/<YYYY-MM>.parquet` with columns
`time` (UTC), `value`, `approval_status`, `qualifier`. Re-pulling a window replaces overlapping rows,
which is how USGS revisions to provisional data get picked up.
"""

from datetime import date, datetime
from pathlib import Path

import pandas as pd

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


def write_obs(root: Path | str, site: str, variable: str, frame: pd.DataFrame) -> list[Path]:
    """Merge `frame` (time, value, approval_status, qualifier) into monthly Parquet files."""
    written = []
    base = Path(root) / "obs" / site_id(site) / variable
    base.mkdir(parents=True, exist_ok=True)
    frame = frame.dropna(subset=["value"])
    for month, part in frame.groupby(frame["time"].dt.strftime("%Y-%m")):
        path = base / f"{month}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            part = pd.concat([old[~old["time"].isin(part["time"])], part], ignore_index=True)
        part.sort_values("time").to_parquet(path, index=False)
        written.append(path)
    return written


def read_obs(root: Path | str, site: str, variable: str) -> pd.DataFrame:
    base = Path(root) / "obs" / site_id(site) / variable
    files = sorted(base.glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=["time", "value", "approval_status", "qualifier"])
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True).sort_values("time").reset_index(drop=True)
