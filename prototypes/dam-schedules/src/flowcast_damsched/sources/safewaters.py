"""Brookfield Renewable "Safe Waters": ~50 FERC-licensed hydro facilities in ME, NH, NY, MA, PA, MD, WV, NC/TN.

* `facility-sitemap.xml` lists the facility pages.
* Each facility page is server-rendered HTML with a "Short-term Schedule" table and a "Long-term Schedule"
  block (free text, PDF calendars, sometimes an .xlsx).
* Every page also embeds `var safeWaterDATA = {...}`: the current operator-reported flow and pool
  elevation ("matrices") for every facility, so one fetch gives observed releases for all of them.

Short-term tables come in several layouts. Parsed here: interval tables (Start, End/Stop, Flow; optional
Station column) and daily tables (Date, cfs). Everything else (generator counts, pool-elevation targets,
whitewater status lists) is kept as a NOTICE row with the table text, which is the long tail an LLM
extraction step would handle. Times are local Eastern (all facilities are in the Eastern time zone).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pandas as pd
from bs4 import BeautifulSoup

from ..fetch import Fetched, Fetcher
from ..schema import EASTERN, Kind, frame

SITEMAP = "https://www.safewaters.com/facility-sitemap.xml"
_LOC = re.compile(r"<loc>([^<]+/facility/[^<]+/)</loc>")
_DATA = "var safeWaterDATA = "
_NOW = re.compile(r"([-\d.,]+)\s*(cfs|ft)?\s*as of\s*(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2} [AP]M)", re.I)
_TIME_FORMATS = ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%y %H:%M", "%m/%d/%Y %H:%M")
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")


def facility_urls(sitemap_xml: str) -> list[str]:
    return _LOC.findall(sitemap_xml)


def _local(text: str, formats=_TIME_FORMATS) -> datetime | None:
    text = re.sub(r"\s+", " ", text.strip().upper())
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=EASTERN)
        except ValueError:
            continue
    return None


def _end(text: str) -> datetime | None:
    t = _local(text)
    # "11:59 PM" means through the end of the day.
    if t is not None and t.hour == 23 and t.minute == 59:
        t = t.replace(hour=0, minute=0) + timedelta(days=1)
    return t


def _cfs(text: str) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def parse_short_term(table, slug: str, fetched: Fetched) -> list[dict]:
    raw_headers = [th.get_text(" ", strip=True) for th in table.select("thead th")] or [
        td.get_text(" ", strip=True) for td in table.select("tr")[0].select("td, th")
    ]
    headers = [h.lower() for h in raw_headers]
    rows = [[td.get_text(" ", strip=True) for td in tr.select("td")] for tr in table.select("tbody tr")]
    rows = [r for r in rows if any(r)]
    base = {"source": "safewaters", "dam": slug, "issue_time": fetched.fetched_at, "fetched_at": fetched.fetched_at}
    out: list[dict] = []
    flow_col = next((i for i, h in enumerate(headers) if "cfs" in h or h.startswith("flow")), None)
    start_col = next((i for i, h in enumerate(headers) if h.startswith("start")), None)
    station_col = next((i for i, h in enumerate(headers) if h == "station"), None)
    if start_col is not None and flow_col is not None:
        # The end column is sometimes unlabeled; it's the one right after Start.
        for r in rows:
            if len(r) <= max(start_col + 1, flow_col):
                continue
            start, end, cfs = _local(r[start_col]), _end(r[start_col + 1]), _cfs(r[flow_col])
            if start and end and cfs is not None:
                dam = f"{slug}:{r[station_col].lower().replace(' ', '-')}" if station_col is not None else slug
                out.append({**base, "dam": dam, "kind": Kind.SCHEDULED_RELEASE, "valid_start": start, "valid_end": end,
                            "value": cfs, "unit": "cfs", "note": None})
        if out:
            return out
    if headers[:1] == ["date"] and flow_col == 1:
        for r in rows:
            day = _local(r[0], _DATE_FORMATS)
            cfs = _cfs(r[1]) if len(r) > 1 else None
            if day and cfs is not None:
                out.append({**base, "kind": Kind.SCHEDULED_RELEASE, "valid_start": day, "valid_end": day + timedelta(days=1),
                            "value": cfs, "unit": "cfs_daily_mean", "note": None})
        if out:
            return out
    text = " | ".join(raw_headers) + "\n" + "\n".join(" | ".join(r) for r in rows)
    return [{**base, "kind": Kind.NOTICE, "valid_start": fetched.fetched_at, "valid_end": fetched.fetched_at,
             "value": None, "unit": None, "note": f"unparsed short-term table:\n{text}"}]


def parse_facility(page: str, slug: str, fetched: Fetched) -> tuple[list[dict], dict]:
    soup = BeautifulSoup(page, "lxml")
    rows: list[dict] = []
    short = soup.select_one("div.short-term-schedules table")
    if short is not None:
        rows += parse_short_term(short, slug, fetched)
    now = []
    for h5 in soup.select("h5"):
        if m := _NOW.search(h5.get_text(" ", strip=True)):
            when = datetime.strptime(m.group(3), "%Y-%m-%d %I:%M:%S %p").replace(tzinfo=EASTERN)
            now.append((float(m.group(1).replace(",", "")), (m.group(2) or "").lower(), when))
    long_term = soup.select_one("div.long-term-schedules")
    links = [a["href"] for a in long_term.select("a[href]")] if long_term else []
    meta = {
        "slug": slug,
        "now": now,
        "long_term_files": sorted({h for h in links if h.lower().endswith((".pdf", ".xlsx", ".xls"))}),
        "long_term_text": long_term.get_text(" ", strip=True)[:2000] if long_term else "",
    }
    return rows, meta


def _facilities(page: str) -> Iterator[dict]:
    i = page.find(_DATA)
    if i < 0:
        return
    data, _ = json.JSONDecoder().raw_decode(page[i + len(_DATA):])
    for region in data["facilities"]:
        for river in region["rivers"]:
            yield from river["facilities"]


def facility_coords(page: str) -> dict[str, tuple[float, float]]:
    out = {}
    for f in _facilities(page):
        try:
            out[f["facility_slug"]] = (float(f["latitude"]), float(f["longitude"]))
        except (TypeError, ValueError):
            continue
    return out


def parse_observed(page: str, fetched: Fetched) -> list[dict]:
    """Operator-reported current values for every facility, from the embedded safeWaterDATA object."""
    rows, seen = [], set()
    for f in _facilities(page):
        for m in f.get("matrices") or []:
            unit = (m.get("unit") or "").lower().replace("ft^3/s", "cfs")
            kind = {"cfs": Kind.OBSERVED_RELEASE, "ft": Kind.POOL_ELEVATION, "feet": Kind.POOL_ELEVATION}.get(unit)
            key = (f["facility_slug"], m.get("description"), unit)
            if kind is None or key in seen:
                continue
            seen.add(key)
            rows.append({
                "source": "safewaters", "dam": f["facility_slug"], "kind": kind, "valid_start": fetched.fetched_at,
                "valid_end": fetched.fetched_at, "value": m.get("value"), "unit": "cfs" if kind == Kind.OBSERVED_RELEASE else "ft",
                "issue_time": fetched.fetched_at, "fetched_at": fetched.fetched_at, "note": m.get("description"),
            })
    return rows


def collect(fetcher: Fetcher, slugs: set[str] | None = None) -> Iterator[tuple[pd.DataFrame, dict]]:
    """One (records, meta) per facility page; the first page's meta also carries every facility's coordinates."""
    urls = facility_urls(fetcher.get(SITEMAP).text)
    first = True
    for url in urls:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slugs and slug not in slugs:
            continue
        got = fetcher.get(url)
        rows, meta = parse_facility(got.text, slug, got)
        if first:
            rows += parse_observed(got.text, got)
            meta["coords"] = facility_coords(got.text)
            first = False
        yield frame(rows), meta
