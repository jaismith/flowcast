"""Season-ahead release calendars published as PDFs where release days are colored cells.

Brookfield's whitewater calendars (Fife Brook, Kennebec/Dead, Rapid/Magalloway, Pontook) and many
utility calendars share this layout: month grids with a weekday header row (SU M TU W TH F SA),
day numbers as text, and a filled rectangle behind each release day. The meaning of a color is in a
legend box of the same color, or only in prose ("releases start 11:30-noon, 3 h at 800 cfs").

`parse_pdf` is generic: it returns (date, color, legend text) for every colored day. Turning a color
into a flow and a time window is per-calendar config (`CalendarSpec`), which is the small piece an
agent writes once per calendar per season and a human can check against the PDF.
"""

from __future__ import annotations

import calendar
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd
import pdfplumber

from ..fetch import Fetched
from ..schema import EASTERN, Kind, frame

MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_name) if m}
_WEEKDAY_FIRST = {"SU", "S", "SUN", "SUNDAY"}
_WEEKDAY_LAST = {"SA", "S", "SAT", "SATURDAY"}


@dataclass(frozen=True)
class Window:
    start: time
    hours: float
    cfs: float


@dataclass(frozen=True)
class CalendarSpec:
    source: str
    dam: str
    url: str
    colors: dict[tuple[float, ...], Window] = field(default_factory=dict)
    # Per-date overrides stated in prose on the calendar (e.g. "*June 27th: release starts at 10 a.m.").
    overrides: dict[date, Window] = field(default_factory=dict)


def _is_color(c) -> bool:
    if not isinstance(c, (tuple, list)) or len(c) != 3:
        return False
    r, g, b = c
    return max(r, g, b) - min(r, g, b) > 0.15  # not white, black or gray


def _key(c) -> tuple[float, ...]:
    return tuple(round(float(x), 3) for x in c)


def _year(text: str) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    if not m:
        raise ValueError("calendar has no year")
    return int(m.group(1))


def parse_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    """Colored days: columns date, color, legend."""
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            # Footnote marks on day numbers ("27*") would otherwise drop the day.
            words = [{**w, "text": w["text"].rstrip("*†")} for w in words]
            year = _year(page.extract_text() or "")
            fills = [r for r in page.rects if _is_color(r.get("non_stroking_color")) and r.get("fill", True)]
            grids = _grids(words)
            day_boxes = []
            for g in grids:
                ndays = calendar.monthrange(year, g["month"])[1]
                for w in words:
                    if not w["text"].isdigit() or not (g["x0"] <= (w["x0"] + w["x1"]) / 2 <= g["x1"]):
                        continue
                    if not (g["top"] < w["top"] < g["bottom"]) or not 1 <= int(w["text"]) <= ndays:
                        continue
                    cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
                    hits = [r for r in fills if r["x0"] <= cx <= r["x1"] and r["top"] <= cy <= r["bottom"]]
                    day_boxes.append((cx, cy))
                    if hits:
                        r = min(hits, key=lambda r: r["width"] * r["height"])
                        out.append((date(year, g["month"], int(w["text"])), _key(r["non_stroking_color"])))
            legends = _legends(page, words, fills, day_boxes)
    df = pd.DataFrame(out, columns=["date", "color"]).drop_duplicates()
    df["legend"] = df["color"].map(lambda c: legends.get(c, ""))
    return df.sort_values("date").reset_index(drop=True)


def _grids(words: list[dict]) -> list[dict]:
    """One box per month: from the weekday header row down to the next header row in the same column."""
    heads = []
    for w in words:
        if w["text"].upper() not in _WEEKDAY_FIRST:
            continue
        row = [v for v in words if abs(v["top"] - w["top"]) < 2 and v["x0"] >= w["x0"] and v["x0"] - w["x0"] < 200]
        last = next((v for v in sorted(row, key=lambda v: v["x0"])[1:] if v["text"].upper() in _WEEKDAY_LAST), None)
        if last is None or len(row) < 7:
            continue
        month = [v for v in words if v["text"].upper() in MONTHS and v["bottom"] <= w["top"] + 1
                 and w["top"] - v["bottom"] < 30 and w["x0"] - 10 <= (v["x0"] + v["x1"]) / 2 <= last["x1"] + 10]
        if month:
            m = max(month, key=lambda v: v["bottom"])
            heads.append({"month": MONTHS[m["text"].upper()], "x0": w["x0"] - 6, "x1": last["x1"] + 6, "top": w["bottom"]})
    for h in heads:
        below = [o["top"] for o in heads if o is not h and o["top"] > h["top"] and o["x0"] < h["x1"] and o["x1"] > h["x0"]]
        h["bottom"] = min(below) - 25 if below else h["top"] + 120
    return heads


def _legends(page, words, fills, day_boxes) -> dict[tuple[float, ...], str]:
    """Text inside colored boxes that aren't day cells."""
    legends: dict[tuple[float, ...], list[str]] = {}
    for r in fills:
        if any(r["x0"] <= x <= r["x1"] and r["top"] <= y <= r["bottom"] for x, y in day_boxes):
            continue
        text = [w["text"] for w in words if r["x0"] <= (w["x0"] + w["x1"]) / 2 <= r["x1"] and r["top"] <= (w["top"] + w["bottom"]) / 2 <= r["bottom"]]
        if text:
            legends.setdefault(_key(r["non_stroking_color"]), []).extend(text)
    return {k: " ".join(v) for k, v in legends.items()}


def to_records(days: pd.DataFrame, spec: CalendarSpec, fetched: Fetched) -> pd.DataFrame:
    rows = []
    for d in days.itertuples():
        win = spec.overrides.get(d.date) or spec.colors.get(d.color)
        if win is None:
            continue
        start = datetime.combine(d.date, win.start, tzinfo=EASTERN)
        rows.append({
            "source": spec.source, "dam": spec.dam, "kind": Kind.RELEASE_DAY, "valid_start": start,
            "valid_end": start + timedelta(hours=win.hours), "value": win.cfs, "unit": "cfs",
            "issue_time": fetched.fetched_at, "fetched_at": fetched.fetched_at, "note": d.legend or None,
        })
    return frame(rows)


FIFE_BROOK_2026 = CalendarSpec(
    source="safewaters_calendar",
    dam="fife-brook",
    url="https://safewaters.com/wp-content/uploads/2026/02/2026-Fife-Brook-Release-Schedule-SafeWaters-Rev-March-2026.pdf",
    # "Releases start between 11:30 a.m. and 12:00 noon ... 3-hour minimum duration at 800 cfs."
    colors={(0.434, 0.828, 0.969): Window(time(11, 30), 3.0, 800.0)},
    overrides={date(2026, 6, 27): Window(time(10, 0), 3.0, 800.0)},
)
