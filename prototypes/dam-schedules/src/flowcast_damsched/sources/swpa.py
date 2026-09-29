"""Southwestern Power Administration projected hourly generation for 18 Corps hydro projects (AR/MO/OK/TX).

https://www.energy.gov/swpa/generation-schedules links one page per weekday (mon.htm ... sun.htm).
Each is a fixed-width <PRE> table: hour-ending (Central time) rows 1-24, one column per project,
values in MW, followed by a project table with units, capacity and approximate full-power discharge.
The pages are overwritten weekly, so fetching all seven gives the last 7 days (as last revised).

Quirks this parser guards against:
* The <TITLE> date is wrong (every page's title carries the current date); the date line in the body is used.
* Column headers don't match the project table (column 9 is "OZK", the table says "OZD"), so columns are
  matched to projects by their number row, not by abbreviation.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pandas as pd

from ..fetch import Fetched, Fetcher
from ..schema import CENTRAL, Kind, frame

INDEX = "https://www.energy.gov/swpa/generation-schedules"
DAY_URL = "https://www.energy.gov/swpa/{day}.htm"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_PRE = re.compile(r"<PRE>(.*?)</PRE>", re.S | re.I)
_BODY_DATE = re.compile(r"PROJECTED LOADING SCHEDULE\s+([A-Z]+)\s+([A-Z]+ +\d{1,2}, +\d{4})")
_TITLE_DATE = re.compile(r"<TITLE>\s*Generation Schedule,\s*([A-Z]+),\s*([A-Z]+ +\d{1,2}, +\d{4})", re.I)
_NUMBERS = re.compile(r"^\s+1\s+2\s+3(\s+\d+)+\s*$")
_HOUR = re.compile(r"^\s*(\d{1,2})((?:\s+\d+)+)\s*$")
_PROJECT = re.compile(r"^\s*(\d{1,2})\s+([A-Z]{3})\s+(.+?)\s{2,}(\S.*?)\s{2,}(\d+)\s+([\d,]+)\s+([\d,]+)\s*$")


class ScheduleFormatError(ValueError):
    pass


def _date(text: str) -> datetime:
    return datetime.strptime(re.sub(r"\s+", " ", text), "%B %d, %Y")


def parse(page: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(hourly MW long table, project table, metadata incl. date checks)."""
    pre = _PRE.search(page)
    if not pre:
        raise ScheduleFormatError("no <PRE> block")
    text = pre.group(1)
    m = _BODY_DATE.search(text)
    if not m:
        raise ScheduleFormatError("no 'PROJECTED LOADING SCHEDULE <weekday> <date>' line")
    weekday, day = m.group(1), _date(m.group(2)).date()
    if day.strftime("%A").upper() != weekday:
        raise ScheduleFormatError(f"weekday {weekday} does not match {day}")
    title = _TITLE_DATE.search(page)
    meta = {"date": day, "title_date": _date(title.group(2)).date() if title else None}

    lines = text.splitlines()
    numbers = next((ln for ln in lines if _NUMBERS.match(ln)), None)
    if numbers is None:
        raise ScheduleFormatError("no column-number row")
    col_ids = [int(x) for x in numbers.split()]

    projects = []
    for ln in lines:
        if p := _PROJECT.match(ln):
            projects.append({
                "lake_no": int(p.group(1)), "abbrev": p.group(2), "project": html.unescape(p.group(3).strip()), "state": p.group(4).strip(),
                "units": int(p.group(5)), "capacity_mw": float(p.group(6).replace(",", "")),
                "full_power_cfs": float(p.group(7).replace(",", "")),
            })
    table = pd.DataFrame(projects)
    if set(col_ids) - set(table.get("lake_no", [])):
        raise ScheduleFormatError(f"columns {sorted(set(col_ids) - set(table['lake_no']))} missing from the project table")
    name_of = table.set_index("lake_no")["project"].to_dict()

    rows = []
    hours_seen = set()
    for ln in lines:
        h = _HOUR.match(ln)
        if not h:
            continue
        # The column-number row also looks like an hour row, but has one value too few.
        hour, values = int(h.group(1)), [int(v) for v in h.group(2).split()]
        if not 1 <= hour <= 24 or len(values) != len(col_ids) or hour in hours_seen:
            continue
        hours_seen.add(hour)
        end = datetime.combine(day, datetime.min.time(), tzinfo=CENTRAL) + timedelta(hours=hour)
        for lake_no, mw in zip(col_ids, values, strict=True):
            rows.append((name_of[lake_no], end - timedelta(hours=1), end, mw))
    if hours_seen != set(range(1, 25)):
        raise ScheduleFormatError(f"expected hours 1-24, got {len(hours_seen)}")
    long = pd.DataFrame(rows, columns=["dam", "valid_start", "valid_end", "value"])
    return long, table, meta


def to_records(long: pd.DataFrame, table: pd.DataFrame, fetched: Fetched) -> pd.DataFrame:
    cap = table.set_index("project")
    rows = [
        {
            "source": "swpa", "dam": r.dam, "kind": Kind.SCHEDULED_GENERATION, "valid_start": r.valid_start,
            "valid_end": r.valid_end, "value": r.value, "unit": "MW", "issue_time": fetched.fetched_at,
            "fetched_at": fetched.fetched_at,
            "note": f"capacity_mw={cap.at[r.dam, 'capacity_mw']:g};full_power_cfs={cap.at[r.dam, 'full_power_cfs']:g}",
        }
        for r in long.itertuples()
    ]
    return frame(rows)


def collect(fetcher: Fetcher) -> Iterator[tuple[pd.DataFrame, dict]]:
    for d in DAYS:
        got = fetcher.get(DAY_URL.format(day=d))
        long, table, meta = parse(got.text)
        yield to_records(long, table, got), {**meta, "url": got.url, "projects": table}


def mw_to_cfs(mw: pd.Series, capacity_mw: float, full_power_cfs: float) -> pd.Series:
    """Nameplate conversion: turbine flow in proportion to load. Ignores head and efficiency curves."""
    return mw / capacity_mw * full_power_cfs
