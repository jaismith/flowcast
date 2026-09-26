"""NYC DEP reservoir releases: the daily release-levels page and the NYC Open Data history.

The page shows one day's release per reservoir (MGD) and is scraped every few hours;
a new version is archived when the date or any value changes.

NYC Open Data "Current Reservoir Levels" (zkky-n5j3) holds daily storage, elevation and
releases from Nov 2017 to Sep 2025, when it stopped updating. Its column field names
are misaligned for Pepacton and Cannonsville (e.g. `cannonsville_release` holds
Pepacton storage), but each column's display name is the DEP SCADA tag, which is
right, so rows are mapped by tag and the tag is kept as the qualifier. A few rows also
carry values swapped between columns; those are kept as published.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime

import pandas as pd

from ..context import Context
from ..model import Issuance
from .common import EASTERN, digest, local_midnight_utc, number, strip_tags

PAGE = "https://www.nyc.gov/site/dep/water/release-levels.page"
# nyc.gov rejects user agents that carry a URL.
PAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; flowcast-archiver)"}
_DATE = re.compile(r"<h1>\s*Release Levels\s*</h1>\s*<p>([^<]+)</p>", re.I)
_CARD = re.compile(r'<div class="release-levels-([a-z]+)">(.*?</div>)\s*</div>', re.S)
_FIELD = re.compile(r'<div class="card-(head|subhead|mgd|ntu)">(.*?)</div>', re.S)

OPENDATA_ID = "zkky-n5j3"
OPENDATA_URL = f"https://data.cityofnewyork.us/resource/{OPENDATA_ID}.json"
OPENDATA_META = f"https://data.cityofnewyork.us/api/views/{OPENDATA_ID}.json"
# SCADA tag (column display name) -> (reservoir, variable). Storage in billion gallons,
# elevation in feet, flows in million gallons per day.
TAGS = {
    "AUGEVolume": ("ashokan_east", "storage_bg"),
    "AUGEASTLEVANALOG": ("ashokan_east", "elevation_ft"),
    "AUGWVOLUME": ("ashokan_west", "storage_bg"),
    "AUGWESTLEVANALOG": ("ashokan_west", "elevation_ft"),
    "ASHREL": ("ashokan", "release_mgd"),
    "SICRESVOLUME": ("schoharie", "storage_bg"),
    "SICRESELEVANALOG": ("schoharie", "elevation_ft"),
    "STPALBFLW": ("schoharie", "shandaken_tunnel_flow_mgd"),
    "RECRESVOLUME": ("rondout", "storage_bg"),
    "RECRESELEVANALOG": ("rondout", "elevation_ft"),
    "RECREL": ("rondout", "release_mgd"),
    "NICRESVOLUME": ("neversink", "storage_bg"),
    "NICRESELEVANALOG": ("neversink", "elevation_ft"),
    "NICNTHFLW": ("neversink", "north_flow_mgd"),
    "NICSTHFLW": ("neversink", "south_flow_mgd"),
    "NICCONFLW": ("neversink", "conservation_flow_mgd"),
    "EDIRESVOLUME": ("pepacton", "storage_bg"),
    "EDIRESELEVANALOG": ("pepacton", "elevation_ft"),
    "EDRNTHFLW": ("pepacton", "north_flow_mgd"),
    "EDRSTHFLW": ("pepacton", "south_flow_mgd"),
    "EDRCONFLW": ("pepacton", "conservation_flow_mgd"),
    "WDIRESVOLUME": ("cannonsville", "storage_bg"),
    "WDIRESELEVANALOG": ("cannonsville", "elevation_ft"),
    "WDRFLW": ("cannonsville", "release_mgd"),
}


def parse_release_page(page_html: str, fetched_at) -> tuple[str, pd.DataFrame]:
    m = _DATE.search(page_html)
    if not m:
        raise ValueError("release-levels page has no date heading")
    as_of = datetime.strptime(strip_tags(m.group(1)), "%B %d, %Y").date()
    rows = []
    for reservoir, body in _CARD.findall(page_html):
        fields = {k: strip_tags(v) for k, v in _FIELD.findall(body)}
        mgd = number(re.sub(r"[^\d.,]", "", fields.get("mgd", "")))
        if mgd is not None:
            rows.append((reservoir, "release_mgd", mgd, fields.get("subhead")))
        if ntu := re.search(r"([\d.]+)\s*ntu", fields.get("ntu", ""), re.I):
            rows.append((reservoir, "turbidity_ntu", float(ntu.group(1)), fields.get("subhead")))
    df = pd.DataFrame(rows, columns=["location_id", "variable", "value", "qualifier"])
    df["dataset"] = "nyc_dep_release"
    df["valid_time"] = local_midnight_utc(as_of)
    df["issue_time"] = fetched_at
    df["fetched_at"] = fetched_at
    return as_of.isoformat(), df


def collect_release_page(ctx: Context) -> Iterator[Issuance]:
    try:
        resp, raw = ctx.get(PAGE, headers=PAGE_HEADERS)
        as_of, frame = parse_release_page(resp.text, raw.fetched_at)
    except Exception:
        ctx.fail("NYC DEP release-levels page failed")
        return
    if frame.empty:
        ctx.fail("NYC DEP release-levels page had no release cards; the layout may have changed")
        return
    values = frame[["location_id", "variable", "value"]].to_csv(index=False)
    key = f"{as_of}/{digest(values)}"
    if not ctx.state.has("nyc_dep_release", key):
        yield Issuance("nyc_dep_release", key, raw.fetched_at, frame, [raw])


def normalize_opendata(records: list[dict], columns: list[dict], fetched_at) -> pd.DataFrame:
    """Rows from the Socrata JSON API plus the view's column metadata (field name -> SCADA tag)."""
    tag_of = {c["fieldName"]: c.get("name") for c in columns}
    frame = pd.DataFrame(records)
    when = pd.to_datetime(frame.pop("neversink_date"))  # the Point_time column
    valid = when.dt.tz_localize(EASTERN, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    parts = []
    for field in frame.columns:
        tag = tag_of.get(field)
        if tag not in TAGS:
            continue
        reservoir, variable = TAGS[tag]
        parts.append(pd.DataFrame({
            "location_id": reservoir, "variable": variable, "qualifier": tag,
            "valid_time": valid, "value": pd.to_numeric(frame[field], errors="coerce"),
        }))
    df = pd.concat(parts, ignore_index=True).dropna(subset=["valid_time", "value"])
    df["dataset"] = "nyc_dep_opendata"
    # Observations: issued when measured.
    df["issue_time"] = df["valid_time"]
    df["fetched_at"] = fetched_at
    return df


def collect_opendata(ctx: Context) -> Iterator[Issuance]:
    """One-time pull of the full Open Data series, one issuance per month of data."""
    meta_resp, meta_raw = ctx.get(OPENDATA_META)
    data_resp, data_raw = ctx.get(OPENDATA_URL, params={"$limit": 50000, "$order": "neversink_date"}, timeout=180)
    df = normalize_opendata(data_resp.json(), meta_resp.json()["columns"], data_raw.fetched_at)
    raws = [meta_raw, data_raw]
    for month, part in df.groupby(df["valid_time"].dt.strftime("%Y-%m")):
        issue = part["valid_time"].min().to_pydatetime()
        yield Issuance("nyc_dep_opendata", f"{OPENDATA_ID}/{month}", issue, part, raws)
        raws = []  # store the full payloads once, with the first month
