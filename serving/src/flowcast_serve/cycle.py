"""Scheduled cycle (01:30, 07:30, 13:30, 19:30 UTC): forecast every active site for the latest synoptic issue.

Active = always-on (pinned or alerts) or visit-active (`awake_until` in the future). Each site's run lock is taken
here, so a wake already running for the same issue is not repeated; the forecast function gets shards of sites.
"""

from __future__ import annotations

import json
import logging

import boto3

from . import config
from .api import emit_metric, flow_model
from .control import Control, is_active, target_issue
from .issues import iso, utcnow
from .registry import served_sites

log = logging.getLogger(__name__)

SHARD_SITES = 8


def run(settings: config.Settings | None = None, issue: str | None = None) -> dict:
    settings = settings or config.Settings()
    now = utcnow()
    issue = issue or target_issue(now)
    if issue > target_issue(now):
        raise ValueError(f"issue {issue} is not available yet (latest is {target_issue(now)})")
    control = Control(settings.table)
    sites = served_sites()
    items = control.sites()
    model = flow_model(settings)
    active = [sid for sid, s in sites.items() if s.forecastable and is_active(s, items.get(sid, {}), now)]
    todo = [sid for sid in active if (items.get(sid, {}).get("last_issue") or "") < issue and control.acquire(sid, issue, model, "cycle", now)]
    client = boto3.client("lambda")
    shards = [todo[i : i + SHARD_SITES] for i in range(0, len(todo), SHARD_SITES)]
    for shard in shards:
        client.invoke(FunctionName=settings.forecast_function, InvocationType="Event",
                      Payload=json.dumps({"action": "forecast", "sites": shard, "issue": issue, "trigger": "cycle", "locked": True, "requested": iso(now)}).encode())
    emit_metric(settings, "ActiveSites", len(active))
    log.info("cycle %s: %d active, %d started in %d shards", issue, len(active), len(todo), len(shards))
    return {"issue": issue, "active": active, "started": todo, "shards": len(shards)}
