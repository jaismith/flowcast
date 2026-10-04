"""`POST /api/visit?site=` and `GET /api/status?site=` (Lambda Function URL behind CloudFront; contract in
serving/schema/api.schema.json). `site` is the id (`USGS-01427510`) or the slug (`callicoon`)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import boto3

from . import config
from .control import Control, is_active, status, target_issue
from .issues import iso, parse_iso, parse_issue, utcnow
from .registry import POINTER, ModelRegistry, ServedSite, resolve, served_sites

log = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.INFO)

_POINTER_TTL_S = 60.0
_pointer: tuple[float, str] | None = None


def forecast_pointer(site_id: str, issue: str | None, now: datetime | None = None) -> dict | None:
    if not issue:
        return None
    t = parse_issue(issue)
    out = {"issue": issue, "issue_time": iso(t), "url": f"/data/v1/sites/{site_id}/forecasts/{issue}.json"}
    if now is not None:
        out["age_h"] = round((now - t).total_seconds() / 3600, 1)
    return out


def flow_model(settings: config.Settings) -> str:
    """The production flow version (run locks are keyed by it), cached for a minute per container."""
    global _pointer
    if _pointer is None or time.monotonic() - _pointer[0] > _POINTER_TTL_S:
        bucket = settings.lake_uri.removeprefix("s3://").split("/", 1)[0]
        doc = ModelRegistry(bucket).read_json(POINTER) or {}
        _pointer = (time.monotonic(), doc.get("flow", "none"))
    return _pointer[1]


def emit_metric(settings: config.Settings, name: str, value: float, unit: str = "Count", **fields) -> None:
    """CloudWatch embedded-metric log line (no PutMetricData calls)."""
    log.info(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{"Namespace": settings.metrics_namespace, "Dimensions": [[]], "Metrics": [{"Name": name, "Unit": unit}]}]}, name: value, **fields}))


def response(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json", "cache-control": "no-store"}, "body": json.dumps(body)}


def start_wake(site: ServedSite, issue: str, model: str, settings: config.Settings, control: Control) -> bool:
    if not control.acquire(site.site_id, issue, model, "wake"):
        return False
    boto3.client("lambda").invoke(
        FunctionName=settings.forecast_function,
        InvocationType="Event",
        Payload=json.dumps({"action": "forecast", "sites": [site.site_id], "issue": issue, "trigger": "wake", "locked": True, "requested": iso(utcnow())}).encode(),
    )
    emit_metric(settings, "Wakes", 1, site=site.site_id, issue=issue)
    return True


def visit(site: ServedSite, settings: config.Settings, control: Control, now: datetime | None = None) -> dict:
    now = now or utcnow()
    pinned = {s for s, v in served_sites().items() if v.pinned}
    item = control.site(site.site_id)
    count = None if site.pinned or is_active(site, item, now) else control.visit_active_count(now, pinned)
    item, capped = control.record_visit(site, now, count)
    target = target_issue(now)
    eta = None
    if not capped and is_active(site, item, now) and (item.get("last_issue") or "") < target:
        if start_wake(site, target, flow_model(settings), settings, control):
            eta = config.WAKE_ETA_S
    run = control.latest_run(site.site_id)
    st = status(site, item, run, now, capped=capped)
    if st.status == "waking" and eta is None:
        started = parse_iso(run.get("started")) or now
        eta = max(5, config.WAKE_ETA_S - int((now - started).total_seconds()))
    emit_metric(settings, "Visits", 1, site=site.site_id, status=st.status)
    return {"id": site.site_id, "status": st.status, "awake_until": iso(st.awake_until), "forecast": forecast_pointer(site.site_id, st.last_issue, now),
            "target_issue": target, "eta_s": eta}


def site_status(site: ServedSite, control: Control, now: datetime | None = None) -> dict:
    now = now or utcnow()
    run = control.latest_run(site.site_id)
    st = status(site, control.site(site.site_id), run, now)
    run_doc = None
    if run:
        run_doc = {"issue": run["sk"].split("#")[1], "status": run.get("status"), "started": run.get("started"), "finished": run.get("finished")}
    return {"id": site.site_id, "status": st.status, "awake_until": iso(st.awake_until), "forecast": forecast_pointer(site.site_id, st.last_issue, now), "run": run_doc}


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = event.get("rawPath", "")
    raw = ((event.get("queryStringParameters") or {}).get("site") or "").strip()
    site = resolve(raw)
    if site is None:
        if re.fullmatch(r"(?i)(USGS-)?\d{8,15}", raw):
            return response(404, {"error": "not_supported", "detail": "flowcast can't forecast this gauge yet: it is not one of the model basins"})
        return response(404, {"error": "unknown_site", "detail": "site must be a USGS id like USGS-01427510 or a site slug"})
    settings = config.Settings()
    control = Control(settings.table)
    if path.endswith("/visit"):
        if method != "POST":
            return response(405, {"error": "method_not_allowed", "detail": "use POST"})
        return response(200, visit(site, settings, control))
    if path.endswith("/status"):
        return response(200, site_status(site, control))
    return response(404, {"error": "no_route", "detail": path})
