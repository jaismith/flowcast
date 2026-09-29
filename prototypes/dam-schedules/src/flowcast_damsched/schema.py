"""The one record shape every source maps to, so providers are interchangeable (rebuild plan §4.1).

A row is a value for one dam over [valid_start, valid_end), as published at `issue_time`.
Sources that don't state an issue time get the fetch time.
"""

from __future__ import annotations

from enum import StrEnum
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")
CENTRAL = ZoneInfo("America/Chicago")


class Kind(StrEnum):
    SCHEDULED_RELEASE = "scheduled_release"  # cfs, planned
    SCHEDULED_GENERATION = "scheduled_generation"  # MW, planned; needs a MW -> cfs conversion
    OBSERVED_RELEASE = "observed_release"  # cfs, operator-reported
    POOL_ELEVATION = "pool_elevation"  # ft
    RELEASE_DAY = "release_day"  # 1 on a calendar release day (value = scheduled cfs when stated)
    NOTICE = "notice"  # free text, value NaN


COLUMNS = ["source", "dam", "kind", "valid_start", "valid_end", "value", "unit", "issue_time", "fetched_at", "note"]


def frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("valid_start", "valid_end", "issue_time", "fetched_at"):
        df[col] = pd.to_datetime(df[col], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def hourly(df: pd.DataFrame, dam: str, kind: Kind) -> pd.Series:
    """Expand interval rows for one dam into an hourly UTC series (latest issue wins where intervals overlap)."""
    rows = df[(df["dam"] == dam) & (df["kind"] == kind)].sort_values("issue_time")
    out: dict[pd.Timestamp, float] = {}
    for r in rows.itertuples():
        for t in pd.date_range(r.valid_start.floor("h"), r.valid_end, freq="h", inclusive="left"):
            out[t] = r.value
    return pd.Series(out, index=pd.DatetimeIndex(list(out), tz="UTC"), dtype=float).sort_index()
