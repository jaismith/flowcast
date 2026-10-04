"""USGS model inputs: hour-ending means of the instantaneous readings, as the training cube built them
(`flowcast_pipeline.dataset.targets.hourly_mean`), for every gauge a forecast reads.

The obs lake (`obs/`, hourly ingest) keeps the reading at the top of each hour for the page; the model was trained
on hour-ending means, and its flat-run and constant-release inputs compare consecutive hours exactly, so forecast
runs keep their own series: `serving/usgs/{gauge}/{variable}/{YYYY-MM}.parquet` (time, value; discharge in m3/s).
Each run re-pulls from a few hours before the newest stored hour, so provisional revisions and late readings land.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd
from flowcast_pipeline.dataset.targets import CFS_TO_M3S, hourly_mean
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.usgs.client import WaterDataClient, WaterDataError
from flowcast_pipeline.usgs.params import Parameter

log = logging.getLogger(__name__)

PARAMS = {"discharge": Parameter.DISCHARGE, "water_temperature": Parameter.WATER_TEMPERATURE, "stage": Parameter.GAGE_HEIGHT}
HISTORY = timedelta(days=45)
OVERLAP = timedelta(hours=6)
# The API caps a query's result pages; a multi-site 45-day pull is split into windows this long.
WINDOW = timedelta(days=12)
TW_RANGE_C = (-1.0, 40.0)


def key(gauge: str, variable: str, month: str) -> str:
    return f"serving/usgs/{gauge}/{variable}/{month}.parquet"


def load(lake: Lake, gauge: str, variable: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Hour-ending means on the hourly [start, end] index (UTC); NaN where missing."""
    index = pd.date_range(start, end, freq="h", tz="UTC")
    frames = []
    for month in pd.period_range(start.tz_convert(None), end.tz_convert(None), freq="M"):
        df = lake.read_parquet(key(gauge, variable, str(month)))
        if df is not None:
            frames.append(df)
    if not frames:
        return pd.Series(np.nan, index=index, dtype=np.float32)
    df = pd.concat(frames).drop_duplicates("time", keep="last")
    s = pd.Series(df["value"].to_numpy(np.float32), index=pd.DatetimeIndex(df["time"]).tz_convert("UTC"))
    return s.reindex(index)


def newest(lake: Lake, gauge: str, variable: str, now: pd.Timestamp) -> pd.Timestamp | None:
    for month in (now, now - pd.Timedelta(days=31)):
        df = lake.read_parquet(key(gauge, variable, month.strftime("%Y-%m")))
        if df is not None and len(df):
            return pd.Timestamp(df["time"].max()).tz_convert("UTC")
    return None


def _store(lake: Lake, gauge: str, variable: str, hourly: pd.DataFrame) -> None:
    for month, part in hourly.groupby(hourly["time"].dt.strftime("%Y-%m")):
        k = key(gauge, variable, month)
        old = lake.read_parquet(k)
        if old is not None:
            part = pd.concat([old[~old["time"].isin(part["time"])], part], ignore_index=True)
        lake.write_parquet(k, part.sort_values("time").reset_index(drop=True))


def refresh(lake: Lake, gauges: dict[str, list[str]], now: pd.Timestamp, client: WaterDataClient | None = None) -> dict[str, list[str]]:
    """Pull new readings for `gauges` ({variable: [usgs ids]}); returns the gauges whose pull failed per variable."""
    client = client or WaterDataClient(timeout_s=60.0, max_retries=3)
    failed: dict[str, list[str]] = {}
    # The current hour is incomplete; only finished hours are stored.
    last_full = now.floor("h")
    for variable, ids in gauges.items():
        ids = sorted(set(ids))
        if not ids:
            continue
        starts = {g: (newest(lake, g, variable, now) or now - HISTORY) - OVERLAP for g in ids}
        start = max(min(starts.values()), now - HISTORY)
        frames = []
        t = start
        try:
            while t < now:
                frames.append(client.continuous_many(ids, PARAMS[variable], t, min(t + WINDOW, now)))
                t += WINDOW
        except WaterDataError as err:
            log.warning("USGS %s pull failed for %s: %s", variable, ids, err)
            failed[variable] = ids
            continue
        iv = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        for g in ids:
            part = iv[iv["monitoring_location_id"] == f"USGS-{g}"] if len(iv) else iv
            hourly = hourly_mean(part.drop(columns="monitoring_location_id") if len(part) else part)
            if hourly.empty:
                continue
            hourly = hourly[hourly["time"] <= last_full][["time", "value"]].copy()
            if variable == "discharge":
                hourly["value"] = hourly["value"] * CFS_TO_M3S
                hourly.loc[hourly["value"] < 0, "value"] = np.nan
            elif variable == "water_temperature":
                bad = (hourly["value"] < TW_RANGE_C[0]) | (hourly["value"] > TW_RANGE_C[1])
                hourly.loc[bad, "value"] = np.nan
            _store(lake, g, variable, hourly.dropna(subset=["value"]))
    return failed
