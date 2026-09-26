"""USGS experimental 7-day water temperature forecast for the upper Delaware.

Issued daily in season (May to Sep 30) as one spreadsheet per issue date, with
a sheet per reservoir-release scenario. Each sheet has a daily-maximum table
("57.8 (53.3-62.3)": median and 90% interval, deg F) and a table of the
probability of exceeding 75 deg F.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import pandas as pd

from ..context import Context
from ..model import Issuance

log = logging.getLogger(__name__)

BASE = "https://labs.waterdata.usgs.gov/water-temperature-forecasts/DRB"
FIRST_SEASON = 2022
LOOKBACK = timedelta(days=21)
_LINK = re.compile(r'href="([^"]*?(\d{4}-\d{2}-\d{2})_DRB_forecast\.xlsx)"')
_CELL = re.compile(r"^\s*([-\d.]+)\s*\(\s*([-\d.]+)\s*-\s*([-\d.]+)\s*\)\s*$")

SITES = {
    "EBDR @ Harvard": "01417500",
    "WBDR @ Hale Eddy": "01426500",
    "DR @ Lordville": "01427207",
    "NR @ Bridgeville": "01436690",
}


def list_spreadsheets(ctx: Context, season: int) -> dict[str, list[str]]:
    """Issue date -> candidate URLs, from the season's archive page."""
    page = f"{BASE}/{season}/archived_forecasts.html"
    try:
        resp, _ = ctx.get(page)
    except Exception:
        log.info("no archive page for %s", season)
        return {}
    out: dict[str, list[str]] = {}
    for href, date in _LINK.findall(resp.text):
        name = href.rsplit("/", 1)[-1]
        candidates = [urljoin(page, href), f"{BASE}/{date[:4]}/spreadsheets/{name}"]
        out.setdefault(date, [])
        out[date] += [c for c in candidates if c not in out[date]]
    return out


def parse_workbook(content: bytes, issue_date: str, fetched_at: datetime) -> pd.DataFrame:
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    issue_time = pd.Timestamp(issue_date, tz="America/New_York").tz_convert("UTC")
    year = int(issue_date[:4])
    rows = []
    for scenario, sheet in sheets.items():
        sites: list[str] = []
        table = None
        for _, r in sheet.iterrows():
            first = str(r.iloc[0]).strip()
            cells = [str(c).strip() for c in r.iloc[1:]]
            if first == "nan" and any(c in SITES or "@" in c for c in cells):
                sites = cells
                continue
            if first == "Date":
                table = "max" if "maximum" in cells[0].lower() else "p75" if "75" in cells[0] else None
                continue
            if table is None or first in ("nan", "") or not sites:
                continue
            try:
                day = pd.Timestamp(datetime.strptime(f"{first.split(', ', 1)[-1]} {year}", "%B %d %Y"))
            except ValueError:
                continue
            if day.month < issue_time.month - 6:
                day = day.replace(year=year + 1)
            for site, cell in zip(sites, cells):
                if site == "nan":
                    continue
                if table == "max" and (m := _CELL.match(cell)):
                    for q, v in zip((0.5, 0.05, 0.95), m.groups()):
                        rows.append((site, "water_temp_max_f", day, q, float(v), scenario))
                elif table == "p75":
                    try:
                        rows.append((site, "prob_water_temp_max_gt_75f", day, None, float(cell), scenario))
                    except ValueError:
                        pass
    df = pd.DataFrame(rows, columns=["location_id", "variable", "valid_time", "quantile", "value", "qualifier"])
    df["valid_time"] = pd.to_datetime(df["valid_time"]).dt.tz_localize("America/New_York").dt.tz_convert("UTC")
    df["usgs_site"] = df["location_id"].map(SITES)
    df["dataset"] = "usgs_drb_temp"
    df["issue_time"] = issue_time
    df["fetched_at"] = fetched_at
    return df


def collect(ctx: Context) -> Iterator[Issuance]:
    cursor = ctx.state.cursor("usgs_drb_temp")
    if cursor is None and ctx.backfill_start is None:
        seasons = range(FIRST_SEASON, ctx.now.year + 1)
        since = None
    else:
        since = (cursor - LOOKBACK) if cursor else ctx.backfill_start
        seasons = range(since.year, ctx.now.year + 1)
    for season in seasons:
        for date, urls in sorted(list_spreadsheets(ctx, season).items()):
            issue_time = pd.Timestamp(date, tz="America/New_York").tz_convert("UTC").to_pydatetime()
            if since and issue_time < since:
                continue
            if ctx.state.has("usgs_drb_temp", date):
                continue
            for url in urls:
                try:
                    resp, raw = ctx.get(url)
                except Exception:
                    continue
                try:
                    frame = parse_workbook(resp.content, date, raw.fetched_at)
                except Exception:
                    log.exception("could not parse %s", url)
                    frame = None
                yield Issuance("usgs_drb_temp", date, issue_time, frame if frame is not None and not frame.empty else None, [raw])
                break
            else:
                log.warning("no reachable spreadsheet for %s", date)
