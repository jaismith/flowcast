"""USACE CWMS Data API: operator-generated reservoir outflow forecasts.

Forecast series are stored unversioned (each model run overwrites), so archiving
each write is the only way to keep what was issued. Every run lists the forecast
outflow series in the catalog, skips RFC/NWS-sourced versions (excluded as model
inputs), and fetches any series whose `last-update` changed; that write time is the
issue time. The catalog itself is snapshotted weekly as a raw payload.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

from ..context import Context
from ..model import Issuance, RawPayload

log = logging.getLogger(__name__)

API = "https://cwms-data.usace.army.mil/cwms-data"
HEADERS = {"Accept": "application/json;version=2"}
# Catalog "like" patterns (regex); parameters are filtered exactly afterwards.
CATALOG_PATTERNS = (r".*Flow-Out.*", r".*Flow-Res.*Out.*")
OUTFLOW_PARAMETERS = {"Flow-Out", "Flow-Res Out", "Flow-Outflow"}
_FORECAST = re.compile(r"fcst|forecast", re.I)
_RFC_SOURCED = re.compile(r"RFC|NWS|WFO|NAEFS|CHIPS|mvrfc", re.I)
ACTIVE_WITHIN = timedelta(days=1)  # series whose data reach at least this close to now
CONTEXT_BEFORE_ISSUE = timedelta(hours=24)
CATALOG_EVERY = timedelta(days=7)
WORKERS = 8
_TO_CFS = {"cfs": 1.0, "kcfs": 1000.0, "cms": 35.3146667}


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def select_series(entries: list[dict], now: datetime) -> list[dict]:
    """Operator forecast outflow series with data extending to about now or later."""
    out = []
    for e in entries:
        parts = e["name"].split(".")
        if len(parts) != 6 or parts[1] not in OUTFLOW_PARAMETERS:
            continue
        version = parts[5]
        if not _FORECAST.search(version) or _RFC_SOURCED.search(version):
            continue
        extents = e.get("extents") or []
        latest = max((_parse_time(x.get("latest-time")) for x in extents if x.get("latest-time")), default=None)
        updated = max((_parse_time(x.get("last-update")) for x in extents if x.get("last-update")), default=None)
        if latest is None or updated is None or latest < now - ACTIVE_WITHIN:
            continue
        out.append({**e, "_latest": latest, "_updated": updated})
    return out


def normalize_values(body: dict, office: str, fetched_at) -> pd.DataFrame:
    scale = _TO_CFS.get((body.get("units") or "").lower())
    if scale is None:
        raise ValueError(f"unexpected units {body.get('units')!r}")
    values = [(t, v) for t, v, *_ in body.get("values") or [] if v is not None]
    location = body["name"].split(".")[0]
    df = pd.DataFrame(values, columns=["valid_time", "value"])
    df["valid_time"] = pd.to_datetime(df["valid_time"], unit="ms", utc=True)
    df["value"] = df["value"] * scale
    df["dataset"] = "cwms_forecast"
    df["location_id"] = f"{office}/{location}"
    df["variable"] = "outflow_cfs"
    df["qualifier"] = body["name"]
    df["fetched_at"] = fetched_at
    return df


def _catalog(ctx: Context) -> tuple[list[dict], list[RawPayload]]:
    entries, raws = [], []
    for pattern in CATALOG_PATTERNS:
        page = None
        while True:
            params = {"like": pattern, "page-size": 1000}
            if page:
                params["page"] = page
            resp, raw = ctx.get(f"{API}/catalog/TIMESERIES", params=params, timeout=120, headers=HEADERS)
            body = resp.json()
            entries += body.get("entries") or []
            raws.append(raw)
            page = body.get("next-page")
            if not page or not body.get("entries"):
                break
    return entries, raws


def collect(ctx: Context) -> Iterator[Issuance]:
    try:
        entries, catalog_raws = _catalog(ctx)
    except Exception:
        ctx.fail("CWMS catalog failed")
        return
    snapshot = ctx.state.cursor("cwms_catalog")
    if snapshot is None or ctx.now - snapshot >= CATALOG_EVERY:
        yield Issuance("cwms_catalog", ctx.now.strftime("%G-W%V"), ctx.now, None, catalog_raws)

    todo = []
    for e in select_series(entries, ctx.now):
        key = f"{e['office']}/{e['name']}/{e['_updated'].isoformat()}"
        if not ctx.state.has("cwms_forecast", key):
            todo.append((key, e))

    def fetch(item):
        key, e = item
        params = {
            "name": e["name"], "office": e["office"], "unit": "EN", "page-size": 20000,
            "begin": (e["_updated"] - CONTEXT_BEFORE_ISSUE).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": e["_latest"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            resp, raw = ctx.get(f"{API}/timeseries", params=params, timeout=60, headers=HEADERS)
            return key, e, resp.json(), raw, None
        except Exception as exc:
            return key, e, None, None, exc

    failures = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for key, e, body, raw, exc in pool.map(fetch, todo):
            if exc is not None:
                failures += 1
                ctx.warn("CWMS %s %s failed: %s", e["office"], e["name"], exc)
                continue
            try:
                frame = normalize_values(body, e["office"], raw.fetched_at)
            except Exception as exc:
                ctx.warn("CWMS %s %s unparseable: %s", e["office"], e["name"], exc)
                continue
            frame["issue_time"] = e["_updated"]
            yield Issuance("cwms_forecast", key, e["_updated"], frame if not frame.empty else None, [raw])
    if todo and failures > len(todo) // 2:
        ctx.fail("CWMS: %d of %d series fetches failed", failures, len(todo))
