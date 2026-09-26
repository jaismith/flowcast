"""NYSDEC thermal-mitigation release requests posted on the ODRM FFMP page.

Each request page has one ramping table per reservoir and day ("8:00 PM, 500 -> 600
cfs"), usually for Cannonsville, a day or so ahead, in summer. Rows are the requested
release after each step, at the step time. Past seasons (2019 onward) stay online and
are backfilled once.

Pages carry no publication timestamp. Pages first seen within two days of their
request date are issued at retrieval time; backfilled pages are issued at local
midnight of the request date and tagged "backfill", which can precede the real
posting by some hours.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from ..context import Context
from ..model import Issuance
from .common import EASTERN, digest, local_midnight_utc, number, strip_tags

INDEX = "https://webapps.usgs.gov/ODRM/ffmp/flexible-flow-management-program"
LIVE_WINDOW = timedelta(days=2)
# After the one-time backfill, only requests this recent are (re)checked.
RECHECK = timedelta(days=14)

_LINK = re.compile(r'<a href="(https://webapps\.usgs\.gov/ODRM/ffmp/[^"]*thermal[^"]*)"[^>]*>\s*([A-Za-z]+ \d{1,2})\s*</a>', re.I)
_BLOCK = re.compile(r"<p[^>]*>(.*?)</p>|<table.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_RESERVOIR = re.compile(r"(Cannonsville|Pepacton|Neversink)\b.*Schedule", re.I)
_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*([AP])\.?\s*M\.?$", re.I)
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DAY_LABEL = re.compile(rf"({_MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", re.I)
_NUMERIC_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_BASE = re.compile(r"(?:above|over) the [^.]*?release of ([\d,]+) cfs", re.I)


@dataclass(frozen=True)
class Request:
    url: str
    day: date

    @property
    def slug(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1]


def list_requests(index_html: str, today: date) -> list[Request]:
    """Requests in index order (newest first within each season, seasons newest first).

    Link text is "July 29" with no year; the year steps back whenever the month/day
    goes up, and a year in the slug overrides that.
    """
    out: list[Request] = []
    year, previous = today.year, None
    for url, label in _LINK.findall(index_html):
        month_day = datetime.strptime(label, "%B %d")
        key = (month_day.month, month_day.day)
        if previous is not None and key > previous:
            year -= 1
        previous = key
        slug_year = re.search(r"-(20\d\d)$", url)
        if slug_year:
            year = int(slug_year.group(1))
        out.append(Request(url, date(year, month_day.month, month_day.day)))
    return out


def _parse_time(text: str) -> time | None:
    text = text.strip().upper().replace(" ", "")
    if text in ("NOON", "12NOON"):
        return time(12)
    if text in ("MIDNIGHT", "12MIDNIGHT"):
        return time(0)
    m = _TIME.match(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)) % 12, int(m.group(2)), m.group(3)
    return time(hour + (12 if ampm == "P" else 0), minute)


def _parse_day(label: str, default_year: int) -> date | None:
    if m := _NUMERIC_DATE.match(label.strip()):
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(year + 2000 if year < 100 else year, month, day)
    if m := _DAY_LABEL.search(label):
        month = datetime.strptime(m.group(1).title(), "%B").month
        return date(int(m.group(3) or default_year), month, int(m.group(2)))
    return None


def parse_request(page_html: str, request_day: date) -> pd.DataFrame:
    """Step schedule rows per reservoir: the requested change at each ramp step, the release after it,
    and the release before the first step.

    Some pages give only the change ("+200"); on single-reservoir pages the absolute release is
    rebuilt from the base stated in the text ("... above the Table 4G Level L2 release of 500 cfs").
    """
    reservoirs = set(_RESERVOIR.findall(strip_tags(page_html)))
    base = _BASE.search(strip_tags(page_html)) if len(reservoirs) == 1 else None
    rows = []
    reservoir, day = None, request_day
    level: dict[str, float] = {}
    started: set[str] = set()
    for block in _BLOCK.finditer(page_html):
        if block.group(1) is not None:
            text = strip_tags(block.group(1))
            if m := _RESERVOIR.search(text):
                reservoir, day = m.group(1).title(), request_day
                if base and reservoir not in level:
                    level[reservoir] = number(base.group(1))
            elif (parsed := _parse_day(text, request_day.year)) is not None and len(text) < 40:
                day = parsed
            continue
        if reservoir is None:
            continue
        previous = None
        for row in _ROW.findall(block.group(0)):
            cells = [strip_tags(c) for c in _CELL.findall(row)] + [""] * 4
            if (t := _parse_time(cells[0])) is None:
                continue
            begin, end, change = number(cells[1]), number(cells[2]), number(cells[3])
            if begin is None and end is None and change is None:
                continue
            when = datetime.combine(day, t, tzinfo=EASTERN)
            if previous is not None and when <= previous:
                when += timedelta(days=1)  # a table that runs past midnight
                day += timedelta(days=1)
            previous = when
            if begin is None:
                begin = level.get(reservoir)
            if end is None and begin is not None and change is not None:
                end = begin + change
            if change is None and begin is not None and end is not None:
                change = end - begin
            if reservoir not in started and begin is not None:
                rows.append((reservoir, "release_before_request_cfs", when, begin))
                started.add(reservoir)
            if change is not None:
                rows.append((reservoir, "requested_change_cfs", when, change))
            if end is not None:
                rows.append((reservoir, "requested_release_cfs", when, end))
                level[reservoir] = end
    df = pd.DataFrame(rows, columns=["location_id", "variable", "valid_time", "value"])
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
    return df


def collect(ctx: Context) -> Iterator[Issuance]:
    try:
        resp, _ = ctx.get(INDEX)
    except Exception:
        ctx.fail("ODRM FFMP index failed")
        return
    requests_ = list_requests(resp.text, ctx.now.date())
    if not requests_:
        ctx.fail("ODRM FFMP index lists no thermal release requests; the layout may have changed")
        return
    backfilled = ctx.state.last_run_at("nysdec_thermal") is not None
    for req in requests_:
        if backfilled and req.day < ctx.now.date() - RECHECK:
            continue
        try:
            page, raw = ctx.get(req.url)
        except Exception:
            ctx.warn("thermal request page %s failed", req.url)
            continue
        key = f"{req.slug}/{digest(page.text)}"
        if ctx.state.has("nysdec_thermal", key):
            continue
        frame = parse_request(page.text, req.day)
        live = raw.fetched_at.date() - req.day <= LIVE_WINDOW
        issue = raw.fetched_at if live else local_midnight_utc(req.day).to_pydatetime()
        if not frame.empty:
            frame["dataset"] = "nysdec_thermal"
            frame["usgs_site"] = None
            frame["qualifier"] = req.slug if live else f"{req.slug};backfill"
            frame["issue_time"] = issue
            frame["fetched_at"] = raw.fetched_at
        yield Issuance("nysdec_thermal", key, issue, frame if not frame.empty else None, [raw])
