"""Entry point of the `flowcast-forecast` container function (infra-v2/lib/serving.ts):
`{"action": "forecast", "sites": [ids], "issue": "YYYYMMDDHH", "trigger": "cycle" | "wake" | "manual", "locked": bool}`
from the cycle and from wakes, and `{"action": "snodas"}` daily."""

from __future__ import annotations

import logging

import pandas as pd
from flowcast_pipeline.lake import Lake

from . import config, forecast, snodas_live
from .issues import utcnow
from .registry import served_sites

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    event = event or {}
    settings = config.Settings()
    if event.get("action") == "snodas":
        basins = [s.usgs_id for s in served_sites().values()]
        return snodas_live.ingest(Lake(settings.lake_uri), basins, pd.Timestamp(utcnow()), days_back=int(event.get("days_back", 10)))
    return forecast.run(event["sites"], event["issue"], event.get("trigger", "manual"), bool(event.get("locked")), settings, event.get("requested"))
