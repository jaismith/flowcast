"""Entry point of the `flowcast-forecast` container function (infra-v2/lib/serving.ts):
`{"action": "forecast", "sites": [ids], "issue": "YYYYMMDDHH", "trigger": "cycle" | "wake" | "manual", "locked": bool}`
from the cycle and from wakes, `{"action": "snodas"}` daily, and `{"action": "static", "basins": [...] | null, "cube": ...}`
(onboarding: builds missing static.json files, throttled, re-invoking itself with the rest before its time runs out)."""

from __future__ import annotations

import json
import logging

import boto3
import pandas as pd
from flowcast_pipeline.lake import Lake

from . import config, forecast, light, snodas_live, static_build
from .issues import utcnow
from .registry import ModelRegistry, served_sites

log = logging.getLogger(__name__)

logging.getLogger().setLevel(logging.INFO)


STATIC_RESERVE_MS = 240_000


def static_batch(event: dict, context, settings: config.Settings) -> dict:
    lake = Lake(settings.lake_uri)
    registry = ModelRegistry(settings.lake_uri.removeprefix("s3://").split("/", 1)[0], cache=f"{settings.work_dir}/models")
    entries = {e["id"]: e for e in light.index_entries(lake)}
    batch = static_build.StaticBatch(lake, registry, event["cube"], entries, served_sites(), float(event.get("min_interval_s", 9.0)))
    todo = batch.pending(event.get("basins"))
    done, failed = [], {}
    while todo and context.get_remaining_time_in_millis() > STATIC_RESERVE_MS:
        b = todo.pop(0)
        err = batch.build_one(b)
        if err:
            failed[b] = err
        else:
            done.append(b)
    if todo:
        boto3.client("lambda").invoke(FunctionName=context.function_name, InvocationType="Event",
                                      Payload=json.dumps({**event, "basins": todo}).encode())
    log.info("static batch: %d built, %d failed, %d handed on; failures %s", len(done), len(failed), len(todo), json.dumps(failed))
    return {"built": done, "failed": failed, "remaining": len(todo)}


def handler(event, context):
    event = event or {}
    settings = config.Settings()
    if event.get("action") == "static":
        return static_batch(event, context, settings)
    if event.get("action") == "snodas":
        basins = [s.usgs_id for s in served_sites().values()]
        return snodas_live.ingest(Lake(settings.lake_uri), basins, pd.Timestamp(utcnow()), days_back=int(event.get("days_back", 10)))
    return forecast.run(event["sites"], event["issue"], event.get("trigger", "manual"), bool(event.get("locked")), settings, event.get("requested"))
