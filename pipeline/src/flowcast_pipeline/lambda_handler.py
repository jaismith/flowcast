"""AWS Lambda entry point for the observation ingest, invoked hourly (and weekly with a 30-day window)."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import timedelta

from .ingest import DEFAULT_WINDOW, ingest
from .lake import Lake
from .sites import ingest_gauges
from .usgs.client import WaterDataClient

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    event = event or {}
    window = timedelta(hours=float(event["window_hours"])) if "window_hours" in event else DEFAULT_WINDOW
    client = WaterDataClient(timeout_s=60.0, max_retries=3)
    report = ingest(Lake(os.environ["LAKE_URI"]), ingest_gauges(), client, window)
    summary = asdict(report)
    logging.info("ingest summary %s", summary)
    if not report.ok:
        raise RuntimeError(f"obs ingest {report.run_id} failed for {report.failed}")
    return summary
