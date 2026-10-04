"""Hourly light build (production-architecture.md §2.4): fresh observations in each active site's live.json, the
site index (`/data/v1/sites.json`), and the `MaxForecastAgeHours` metric over always-on sites.

Only active sites (always-on, visited, or woken) get the hourly observation refresh: one multi-site USGS request per
variable. Snoozed sites keep their last live.json until a visit wakes them, which refreshes it with the forecast.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import pandas as pd
from flowcast_pipeline.lake import Lake

from . import bundles, config, eligibility, usgs_inputs
from .api import forecast_pointer
from .control import Control, is_active, status
from .issues import iso, parse_issue, utcnow
from .registry import INDEX_KEY, ServedSite, served_sites

log = logging.getLogger(__name__)

OBS_DAYS = 30
DEFAULT_SITE = "USGS-01427510"
CFS_PER_M3S = 35.314666721


def index_entries(lake: Lake) -> list[dict]:
    data = lake.read(INDEX_KEY)
    return json.loads(data)["sites"] if data else []


def observations(lake: Lake, site: ServedSite, now: pd.Timestamp) -> dict[str, pd.Series]:
    start, end = (now - pd.Timedelta(days=OBS_DAYS)).floor("h"), now.floor("h")
    q = usgs_inputs.load(lake, site.usgs_id, "discharge", start, end) * CFS_PER_M3S
    out = {"discharge": q}
    for var in ("water_temperature", "stage"):
        s = usgs_inputs.load(lake, site.usgs_id, var, start, end)
        if s.notna().any():
            out[var] = s
    return out


def publish_site(lake: Lake, data: bundles.DataBucket, control: Control, site: ServedSite, now: datetime, refresh: bool = True) -> dict:
    """Write the site's live.json from the control table, its forecasts and observations."""
    ts = pd.Timestamp(now)
    if refresh:
        usgs_inputs.refresh(lake, {"discharge": [site.usgs_id], "water_temperature": [site.usgs_id] if site.has_temperature else [], "stage": [site.usgs_id]}, ts)
    item = control.site(site.site_id)
    st = status(site, item, control.latest_run(site.site_id), now)
    keys = data.list_forecasts(site.site_id)[:120]
    recent = [forecast_pointer(site.site_id, k, now) for k in keys]
    info = json.loads(lake.read(f"sites/USGS-{site.usgs_id}/serving/site.json") or b"{}")
    newest = data.get(f"{bundles.site_prefix(site.site_id)}/forecasts/{keys[0]}.json") if keys else None
    obs = observations(lake, site, ts)
    fresh = {"observations": iso(obs["discharge"].dropna().index[-1].to_pydatetime()) if obs["discharge"].notna().any() else None}
    if newest:
        fresh |= {"hrrr": newest["inputs"].get("hrrr_latest"), "mrms": newest["inputs"].get("mrms_latest"), "gefs_init": newest["inputs"].get("gefs_init"), "snodas": newest["inputs"].get("snodas_date")}
    doc = bundles.live_document(site, now, st, obs, recent[0] if recent else None, recent, fresh, info.get("flood_categories", []))
    data.put(f"{bundles.site_prefix(site.site_id)}/live.json", doc, bundles.LIVE_CACHE)
    return doc


def sites_document(entries: list[dict], sites: dict[str, ServedSite], items: dict[str, dict], now: datetime) -> dict:
    out = []
    for e in entries:
        site = sites.get(e["id"])
        item = items.get(e["id"], {})
        last = item.get("last_issue")
        ready = bool(last)
        st = status(site, item, None, now).status if site else None
        out.append({**e, "forecastable": e.get("forecastable", True), "not_forecastable_reason": e.get("not_forecastable_reason"),
                    "forecast_ready": ready, "forecast_issued_at": iso(parse_issue(last)) if last else None, "status": st,
                    "always_on": bool(site and (site.pinned or int(item.get("alerts", 0) or 0) > 0)),
                    "live_url": f"/{bundles.site_prefix(e['id'])}/live.json" if ready else None})
    return {"schema": "flowcast.sites/v1", "generated": iso(now), "default": DEFAULT_SITE, "sites": out}


GAUGES_CACHE = "public, max-age=3600"


def publish_gauges(data: bundles.DataBucket, index: dict, tiles: dict[str, dict]) -> None:
    for k, t in tiles.items():
        data.put(f"data/v1/gauges/tiles/{k}.json", t, GAUGES_CACHE)
    data.put("data/v1/gauges/ids.json", eligibility.id_map(tiles, index["generated"]), GAUGES_CACHE)
    data.put("data/v1/gauges/index.json", index, GAUGES_CACHE)


def refresh_gauges(settings: config.Settings | None = None) -> dict:
    """Daily: the national gauge catalog with each gauge's eligibility and 00060/00010 flags."""
    settings = settings or config.Settings()
    now = pd.Timestamp(utcnow())
    table = eligibility.build({s.usgs_id for s in served_sites().values()}, now)
    index, tiles = eligibility.documents(table, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    publish_gauges(bundles.DataBucket(settings.data_bucket, settings.data_prefix), index, tiles)
    lake = Lake(settings.lake_uri)
    entries = eligibility.index_flags(table, index_entries(lake))
    if entries:
        lake.write(INDEX_KEY, json.dumps({"sites": entries}, indent=0).encode(), "application/json")
    return {**index["counts"], "not_forecastable": sorted(e["id"] for e in entries if not e["forecastable"])}


def run(settings: config.Settings | None = None) -> dict:
    settings = settings or config.Settings()
    now = utcnow()
    lake = Lake(settings.lake_uri)
    data = bundles.DataBucket(settings.data_bucket, settings.data_prefix)
    control = Control(settings.table)
    sites = served_sites()
    items = control.sites()
    active = [s for sid, s in sites.items() if is_active(s, items.get(sid, {}), now)]
    t0 = time.monotonic()
    if active:
        usgs_inputs.refresh(lake, {"discharge": [s.usgs_id for s in active], "water_temperature": [s.usgs_id for s in active if s.has_temperature],
                                   "stage": [s.usgs_id for s in active]}, pd.Timestamp(now))
    for s in active:
        try:
            publish_site(lake, data, control, s, now, refresh=False)
        except Exception:
            log.exception("live.json for %s failed", s.site_id)
    entries = [e for e in (index_entries(lake) or [])] or [{"id": s.site_id, "slug": s.slug, "name": s.name, "river": "", "town": s.short_name, "state": "",
                                                           "lat": s.lat, "lon": s.lon, "area_mi2": 0.0, "has_temp": s.has_temperature, "in_training_region": True} for s in sites.values()]
    data.put("data/v1/sites.json", sites_document(entries, sites, items, now), bundles.LIVE_CACHE)
    ages = [(now - parse_issue(items[s.site_id]["last_issue"])).total_seconds() / 3600 for s in sites.values() if s.pinned and items.get(s.site_id, {}).get("last_issue")]
    pinned_missing = [s.site_id for s in sites.values() if s.pinned and not items.get(s.site_id, {}).get("last_issue")]
    max_age = max(ages + ([99.0] if pinned_missing else []), default=0.0)
    log.info(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{"Namespace": settings.metrics_namespace, "Dimensions": [[]],
             "Metrics": [{"Name": "MaxForecastAgeHours", "Unit": "None"}, {"Name": "LightBuilds", "Unit": "Count"}]}]}, "MaxForecastAgeHours": round(max_age, 2), "LightBuilds": 1}))
    return {"active": [s.site_id for s in active], "max_forecast_age_h": round(max_age, 2), "seconds": round(time.monotonic() - t0, 1)}
