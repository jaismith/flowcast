"""Per-site JSON for the page (serving/schema/) and the alerts hook's per-issue event summary."""

from __future__ import annotations

import json
from datetime import datetime

import boto3
import numpy as np
import pandas as pd

from .issues import iso
from .registry import ServedSite

Q = (5, 25, 50, 75, 95)
DATA_ROOT = "data/v1"
LIVE_CACHE = "public, max-age=60"
IMMUTABLE = "public, max-age=31536000, immutable"
F_70, F_75 = (70 - 32) / 1.8, (75 - 32) / 1.8
SNOW_T_C = 0.5


def site_prefix(site_id: str) -> str:
    return f"{DATA_ROOT}/sites/{site_id}"


def forecast_url(site_id: str, issue_key: str) -> str:
    return f"/{site_prefix(site_id)}/forecasts/{issue_key}.json"


def _r(x, nd: int = 1):
    x = np.asarray(x, dtype=float)
    return [None if not np.isfinite(v) else round(float(v), nd) for v in x]


def quantiles(samples: np.ndarray, nd: int = 1) -> dict[str, list]:
    qs = np.nanpercentile(samples, Q, axis=1)
    return {f"q{q:02d}": _r(v, nd) for q, v in zip(Q, qs)}


def flood_probability(samples_cfs: np.ndarray, issue: pd.Timestamp, categories: list[dict], timezone: str) -> tuple[dict, str]:
    """P(flow exceeds each category's flow at some hour of local day k), days 0-6; and the highest median category."""
    hours = issue + pd.to_timedelta(np.arange(1, samples_cfs.shape[0] + 1), unit="h")
    local = (hours - pd.Timedelta(hours=1)).tz_convert(timezone)
    day = (local.normalize() - (issue.tz_convert(timezone)).normalize()).days.to_numpy()
    out, top = {}, "none"
    for c in categories:
        flow = c.get("flow_cfs")
        if not flow or flow <= 0:
            continue
        exceed = samples_cfs > flow
        out[c["category"]] = [round(float(exceed[day == k].any(axis=0).mean()), 3) if (day == k).any() else 0.0 for k in range(7)]
        if np.median(samples_cfs, axis=1).max() > flow:
            top = c["category"]
    return out, top


def forecast_document(site: ServedSite, issue: pd.Timestamp, issue_key: str, created: datetime, trigger: str, models: dict, flow_cfs: np.ndarray,
                      temp_hourly: np.ndarray | None, temp_daily: tuple[np.ndarray, np.ndarray, np.ndarray] | None, weather: dict, inputs: dict,
                      categories: list[dict], n_flow: int, n_temp: int) -> dict:
    start = iso(issue + pd.Timedelta(hours=1))
    flow = {"unit": "ft3/s", "start": start, "step_h": 1, **quantiles(flow_cfs, 1), "calibrated": True, "n_samples": n_flow}
    med = np.median(flow_cfs, axis=1)
    i = int(np.argmax(med))
    crest = {"time": iso(issue + pd.Timedelta(hours=i + 1)), **{k: float(np.round(np.percentile(flow_cfs[i], q), 1)) for k, q in (("q25", 25), ("q50", 50), ("q75", 75))}}
    trend = "steady" if abs(med[min(23, len(med) - 1)] - med[0]) < 0.05 * max(med[0], 1.0) else ("rising" if med[min(23, len(med) - 1)] > med[0] else "falling")
    probs, top = flood_probability(flow_cfs, issue, categories, site.timezone)
    doc = {
        "schema": "flowcast.forecast/v1", "id": site.site_id, "slug": site.slug, "issue": issue_key, "issue_time": iso(issue), "created": iso(created),
        "run_type": "operational", "trigger": trigger, "models": models, "flow": flow,
        "temperature": None, "weather": weather, "inputs": inputs,
        "story": {"crest": crest, "trend": trend, "max_flood_category": top if categories else None, "flood_probability": probs},
    }
    if temp_hourly is not None:
        days = []
        if temp_daily is not None:
            maxima, dates, lead_days = temp_daily
            for j in range(len(lead_days)):
                v = maxima[j]
                if not np.isfinite(v).any():
                    continue
                q = np.nanpercentile(v, Q)
                days.append({"date": str(pd.Timestamp(dates[j]).date()), "lead_day": int(lead_days[j]), **{f"q{p:02d}": round(float(x), 2) for p, x in zip(Q, q)},
                             "p_above_70f": round(float(np.mean(v > F_70)), 3), "p_above_75f": round(float(np.mean(v > F_75)), 3)})
        doc["temperature"] = {"hourly": {"unit": "degC", "start": start, "step_h": 1, **quantiles(temp_hourly, 2)}, "daily_max": days, "calibrated": True, "n_samples": n_temp}
    return doc


def weather_block(issue: pd.Timestamp, init: pd.Timestamp, gefs_basin: np.ndarray, leads: np.ndarray, past_mrms: pd.Series, snowpack_swe: float | None,
                  snowmelt_bins: list[float] | None) -> dict:
    """GEFS member-mean 6 h bins from the issue (rain, snow by 2 m temperature, 90th percentile precipitation, air
    temperature) and the past 72 h of MRMS in 6 h bins."""
    offset = (issue - init) / pd.Timedelta(hours=1)
    n_bins = int(min(168, leads[-1] - offset) // 6)
    hours = offset + np.arange(1, 6 * n_bins + 1)
    k = np.searchsorted(leads, hours)
    precip = np.nan_to_num(gefs_basin[:, k, 0])  # [member, hour] mm/h
    temp = np.stack([np.interp(hours, leads, gefs_basin[m, :, 1]) for m in range(gefs_basin.shape[0])])
    snow = np.where(temp <= SNOW_T_C, precip, 0.0)
    rain = precip - snow
    bins = lambda x: x.reshape(x.shape[0], n_bins, 6).sum(axis=2)  # noqa: E731
    total = bins(precip)
    past = past_mrms.iloc[-72:]
    past_bins = past.to_numpy(float).reshape(-1, 6).sum(axis=1) if len(past) == 72 else np.full(12, np.nan)
    return {
        "gefs_init": iso(init), "members": int(gefs_basin.shape[0]),
        "bins": {"start": iso(issue + pd.Timedelta(hours=6)), "step_h": 6, "rain_mm": _r(bins(rain).mean(axis=0), 2), "snow_mm": _r(bins(snow).mean(axis=0), 2),
                 "snowmelt_mm": _r(snowmelt_bins, 2) if snowmelt_bins is not None else [None] * n_bins,
                 "precip_p90_mm": _r(np.percentile(total, 90, axis=0), 2), "air_temp_c": _r(temp.reshape(temp.shape[0], n_bins, 6).mean(axis=(0, 2)), 1)},
        "past_rain": {"unit": "mm", "start": iso(issue - pd.Timedelta(hours=66)), "step_h": 6, "values": _r(past_bins, 2)},
        "snowpack_swe_mm": None if snowpack_swe is None or not np.isfinite(snowpack_swe) else round(float(snowpack_swe), 1),
        "snowmelt_total_mm": None if snowmelt_bins is None else round(float(np.sum(snowmelt_bins)), 1),
    }


def event_summary(site: ServedSite, issue_key: str, doc: dict) -> dict:
    """The alerts hook's per-issue summary (`events/{site}/{issue}.json`, EventBridge `flowcast.forecast.issued`)."""
    days = (doc.get("temperature") or {}).get("daily_max") or []
    return {
        "id": site.site_id, "issue": issue_key, "issue_time": doc["issue_time"], "models": doc["models"],
        "flood_probability": doc["story"]["flood_probability"], "max_flood_category": doc["story"]["max_flood_category"],
        "crest": doc["story"]["crest"],
        "warm_water": [{"date": d["date"], "p_above_70f": d["p_above_70f"], "p_above_75f": d["p_above_75f"]} for d in days],
    }


def series(s: pd.Series, unit: str, nd: int) -> dict:
    return {"unit": unit, "start": iso(s.index[0].to_pydatetime()), "step_h": 1, "values": _r(s.to_numpy(float), nd)}


def live_document(site: ServedSite, generated: datetime, status, obs: dict[str, pd.Series], forecast: dict | None, recent: list[dict],
                  freshness: dict, categories: list[dict]) -> dict:
    q, tw, st = obs.get("discharge"), obs.get("water_temperature"), obs.get("stage")
    last = q.dropna() if q is not None else pd.Series(dtype=float)
    now = {"observed_at": None, "flow_cfs": None, "flow_change_24h_cfs": None, "stage_ft": None, "water_temp_c": None, "gauge_stale": True, "flood_category": None}
    if len(last):
        t = last.index[-1]
        prev = q.get(t - pd.Timedelta(hours=24))
        now.update({"observed_at": iso(t.to_pydatetime()), "flow_cfs": round(float(last.iloc[-1]), 1),
                    "flow_change_24h_cfs": None if prev is None or not np.isfinite(prev) else round(float(last.iloc[-1] - prev), 1),
                    "gauge_stale": (pd.Timestamp(generated) - t) > pd.Timedelta(hours=3)})
        if categories:
            now["flood_category"] = "none"
            for c in sorted(categories, key=lambda c: c["stage_ft"]):
                stage = st.dropna().iloc[-1] if st is not None and st.notna().any() else None
                if stage is not None and stage >= c["stage_ft"]:
                    now["flood_category"] = c["category"]
    if st is not None and st.notna().any():
        now["stage_ft"] = round(float(st.dropna().iloc[-1]), 2)
    if tw is not None and tw.notna().any():
        now["water_temp_c"] = round(float(tw.dropna().iloc[-1]), 2)
    return {
        "schema": "flowcast.live/v1", "id": site.site_id, "slug": site.slug, "generated": iso(generated), "status": status.status,
        "always_on": status.always_on, "always_on_reasons": status.always_on_reasons, "awake_until": iso(status.awake_until),
        "forecast": forecast, "recent_forecasts": recent, "static_url": f"/{site_prefix(site.site_id)}/static.json", "now": now,
        "observations": {name: series(s, unit, nd) for name, unit, nd in (("discharge", "ft3/s", 1), ("water_temperature", "degC", 2), ("stage", "ft", 2)) if (s := obs.get(name)) is not None},
        "freshness": freshness,
    }


def static_document(site: ServedSite, entry: dict, statics: dict, categories: list[dict], nws_lid: str | None) -> dict:
    return {
        "schema": "flowcast.static/v1", "id": site.site_id, "slug": site.slug, "name": entry.get("name", site.name), "short_name": site.short_name,
        "river": entry.get("river"), "lat": site.lat, "lon": site.lon, "timezone": site.timezone, "has_temp": bool(entry.get("has_temp")),
        "nws_lid": nws_lid, "usgs_url": f"https://waterdata.usgs.gov/monitoring-location/{site.site_id}/",
        "basin": {k: statics.get(k) for k in ("area_km2", "elevation_m", "forest_frac", "developed_frac", "frac_snow")} | {
            "area_sq_mi": entry.get("area_mi2"), "below_dam": bool(statics.get("below_dam", 0) > 0), "n_major_dams": statics.get("nid_n_major")},
        "flood_categories": categories, "geometry": None, "watershed_description": None,
    }


class DataBucket:
    """Writer for the public JSON (served at /data/*)."""

    def __init__(self, bucket: str, prefix: str = ""):
        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix

    def put(self, key: str, doc: dict, cache_control: str, retention: str | None = None) -> None:
        extra = {"Tagging": f"retention={retention}"} if retention else {}
        self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}{key}", Body=json.dumps(doc, separators=(",", ":")).encode(),
                           ContentType="application/json", CacheControl=cache_control, **extra)

    def get(self, key: str) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}{key}")["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None

    def list_forecasts(self, site_id: str) -> list[str]:
        prefix = f"{self.prefix}{site_prefix(site_id)}/forecasts/"
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out += [o["Key"].rsplit("/", 1)[-1].removesuffix(".json") for o in page.get("Contents", [])]
        return sorted(out, reverse=True)
