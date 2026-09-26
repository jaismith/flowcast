"""Hourly discharge and water temperature from the USGS Water Data API (via `flowcast_pipeline.usgs`).

Hourly values are the mean of the instantaneous readings in the hour ending at the timestamp,
(t - 1 h, t], matching AORC's hour-ending precipitation. `n_obs` and the approved fraction are kept so
training can down-weight thin or provisional hours.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from ..usgs.cache import ResponseCache
from ..usgs.client import WaterDataClient, WaterDataError
from ..usgs.params import Parameter

log = logging.getLogger(__name__)

CFS_TO_M3S = 0.028316846592
VARIABLES = {"discharge": Parameter.DISCHARGE, "water_temperature": Parameter.WATER_TEMPERATURE}


def hourly_mean(iv: pd.DataFrame) -> pd.DataFrame:
    """Hour-ending mean of instantaneous values, with reading count and approved fraction."""
    if iv.empty:
        return pd.DataFrame(columns=["time", "value", "n_obs", "approved_frac"])
    df = iv.dropna(subset=["value"]).drop_duplicates("time")
    # Concatenated yearly chunks can leave `time` as object dtype when some years are empty.
    df = df.assign(time=pd.to_datetime(df["time"], utc=True, format="ISO8601"))
    hour = df["time"].dt.ceil("h")
    grouped = df.assign(hour=hour, approved=(df["approval_status"] == "Approved").astype(float)).groupby("hour")
    out = pd.DataFrame(
        {
            "value": grouped["value"].mean(),
            "n_obs": grouped["value"].size().astype(np.int16),
            "approved_frac": grouped["approved"].mean().astype(np.float32),
        }
    )
    out.index.name = "time"
    return out.reset_index()


def pull_site(client: WaterDataClient, site: str, variable: str, start: pd.Timestamp, end: pd.Timestamp, out_dir: Path) -> int:
    path = out_dir / variable / f"{site}_{start:%Y%m%d}.parquet"
    if path.exists():
        return len(pd.read_parquet(path, columns=["value"]))
    # The API allows windows up to 3 years; one paged window costs fewer requests than per-year cached chunks.
    single_window = end - start < pd.Timedelta(days=3 * 365 - 7)
    iv = client.continuous(site, VARIABLES[variable], start, end, use_cache=not single_window)
    hourly = hourly_mean(iv)
    if variable == "discharge":
        hourly["value"] = hourly["value"] * CFS_TO_M3S
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    hourly.to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(hourly)


def pull_all(jobs: list[tuple[str, str, pd.Timestamp]], end: pd.Timestamp, out_dir: Path, cache_dir: Path, workers: int = 4, min_interval_s: float = 0.0) -> dict:
    """`jobs` are (site, variable, start) in priority order. Returns hourly row counts (-1 = failed).

    The API allows 1,000 requests/hour per key, shared with the production hourly ingest; `min_interval_s` per
    worker keeps bulk pulls under that (8 workers x 40 s = 720/hour).
    """
    results: dict[tuple[str, str, pd.Timestamp], int] = {}

    def run(site: str, variable: str, start: pd.Timestamp) -> int:
        client = WaterDataClient(cache=ResponseCache(cache_dir), max_retries=8, min_interval_s=min_interval_s)
        return pull_site(client, site, variable, start, end, out_dir)

    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, *job): job for job in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as exc:  # one bad site must not stop a multi-hour pull; failures are retried next pass
                log.warning("%s %s from %s failed: %s", *key, exc)
                results[key] = -1
            if i % 25 == 0:
                log.info("targets %d/%d", i, len(futures))
    return results


def load_usgs(out_dir: Path, site: str, variable: str) -> pd.DataFrame:
    """All pulled windows for a site, later pulls winning where they overlap."""
    files = sorted((out_dir / variable).glob(f"{site}_*.parquet"))
    if not files:
        return pd.DataFrame(columns=["value", "n_obs", "approved_frac"], index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = pd.concat([pd.read_parquet(f) for f in files]).drop_duplicates("time", keep="last")
    return df.set_index("time").sort_index()
