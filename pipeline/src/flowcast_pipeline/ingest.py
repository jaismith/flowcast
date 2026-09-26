"""Hourly observation ingest into the lake (plan milestone 0.2).

Each run pulls a trailing window (12 h by default, so a few missed runs heal themselves) of discharge,
water temperature and stage for every registry gauge, one multi-site request per parameter, and merges
the hourly values into `obs/`. A weekly run with a 30-day window picks up USGS revisions.

Every run leaves a marker `_runs/obs-ingest/<YYYY-MM-DD>/<run_id>-<ok|failed>.json`; `ingest_health`
reads the marker names (no object reads) to count missed hourly cycles.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import pandas as pd

from .lake import Lake
from .obs import VARIABLE_PARAMETERS, to_hourly, write_obs
from .usgs.client import WaterDataClient, WaterDataError

log = logging.getLogger(__name__)

VARIABLES = ("discharge", "water_temperature", "stage")
DEFAULT_WINDOW = timedelta(hours=12)
RUNS_PREFIX = "_runs/obs-ingest"


@dataclass
class IngestReport:
    run_id: str
    window_start: str
    window_end: str
    rows: dict[str, int] = field(default_factory=dict)
    gauges: dict[str, int] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)
    files: int = 0
    failed: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


def ingest(
    lake: Lake,
    gauges: list[str],
    client: WaterDataClient,
    window: timedelta = DEFAULT_WINDOW,
    now: pd.Timestamp | None = None,
    variables: tuple[str, ...] = VARIABLES,
) -> IngestReport:
    started = time.monotonic()
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    start = now - window
    report = IngestReport(run_id=f"{now:%Y%m%dT%H%M%SZ}", window_start=start.isoformat(), window_end=now.isoformat())
    for variable in variables:
        try:
            iv = client.continuous_many(gauges, VARIABLE_PARAMETERS[variable], start, now)
        except WaterDataError as exc:
            log.error("USGS %s request failed: %s", variable, exc)
            report.failed.append(variable)
            continue
        rows = 0
        for site, g in iv.groupby("monitoring_location_id"):
            hourly = to_hourly(g.drop(columns="monitoring_location_id"))
            report.files += len(write_obs(lake, site, variable, hourly))
            rows += int(hourly["value"].notna().sum())
        report.rows[variable] = rows
        report.gauges[variable] = int(iv["monitoring_location_id"].nunique())
        if not iv.empty:
            report.latest[variable] = iv["time"].max().isoformat()
    report.seconds = round(time.monotonic() - started, 1)
    status = "ok" if report.ok else "failed"
    lake.write(f"{RUNS_PREFIX}/{now:%Y-%m-%d}/{report.run_id}-{status}.json", json.dumps(asdict(report)).encode(), "application/json")
    return report


def ingest_health(lake: Lake, now: pd.Timestamp | None = None, days: int = 7) -> dict:
    """Hourly cycles in the trailing `days` with no successful run, from the run markers."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    first_day = (now - pd.Timedelta(days=days)).floor("D")
    ok_hours, failed_runs = set(), 0
    for day in pd.date_range(first_day, now.floor("D"), freq="D"):
        for key in lake.list(f"{RUNS_PREFIX}/{day:%Y-%m-%d}/"):
            name = key.rsplit("/", 1)[-1]
            stamp, _, status = name.removesuffix(".json").rpartition("-")
            if status == "ok":
                ok_hours.add(pd.Timestamp(stamp).floor("h"))
            else:
                failed_runs += 1
    first_run = min(ok_hours) if ok_hours else None
    window_start = max(now.floor("h") - pd.Timedelta(days=days), first_run) if first_run is not None else now.floor("h")
    # The current hour's run may not have happened yet.
    expected = pd.date_range(window_start, now.floor("h") - pd.Timedelta(hours=1), freq="h")
    missed = [h for h in expected if h not in ok_hours]
    return {
        "window_days": days,
        "first_run": first_run.isoformat() if first_run is not None else None,
        "last_ok_run": max(ok_hours).isoformat() if ok_hours else None,
        "expected_cycles": len(expected),
        "missed_cycles": len(missed),
        "missed_fraction": len(missed) / len(expected) if len(expected) else None,
        "failed_runs": failed_runs,
        "days_covered": round(len(expected) / 24, 2),
    }
