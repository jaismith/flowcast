"""MARFC river forecasts from RVF text bulletins in the IEM AFOS archive.

The same forecasts NWPS serves as JSON, but every issuance since 2000 is kept
by the Iowa Environmental Mesonet, so this doubles as the historical backfill
and as a catch-up for issuances NWPS replaced between polls. Bulletins carry
stage (or reservoir pool elevation) only; flow is derived with the current USGS
rating and tagged with the rating id, so older issuances carry rating error.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..context import Context
from ..model import Issuance, RawPayload
from . import rating

log = logging.getLogger(__name__)

URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
BACKFILL_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
OVERLAP = timedelta(days=2)
CHUNK = timedelta(days=366)
MAX_CHUNKS_PER_RUN = 4

# SHEF time zone codes seen in MARFC bulletins.
_TZ = {
    "Z": timezone.utc,
    "E": ZoneInfo("America/New_York"),
    "ES": timezone(timedelta(hours=-5)),
    "ED": timezone(timedelta(hours=-4)),
}
_PE_VARIABLE = {"HG": "stage_ft", "HP": "pool_elev_ft", "QR": "flow_cfs", "QT": "flow_cfs"}
_HEADER = re.compile(r"^\.ER?\s+(\w+)\s+(\d{4,8})\s+([A-Z]{1,2})\s+(.*)$")
_CONTINUATION = re.compile(r"^\.ER?\d+\s?(.*)$")


@dataclass
class EMessage:
    lid: str
    pe: str
    created: datetime
    values: list[tuple[datetime, float]]


def split_products(text: str) -> list[str]:
    # AFOS text uses \r\r\n line ends; splitlines() would turn each into a blank line
    # between a .E header and its .E1/.E2 continuations.
    text = text.replace("\r", "")
    return [p.strip("\x03\n ") for p in text.split("\x01") if p.strip("\x03\n ")]


def _strip_comments(line: str) -> str:
    return "".join(line.split(":")[0::2])


def _resolve_date(token: str, reference: datetime) -> tuple[int, int, int]:
    """SHEF dates are MMDD, YYMMDD or CCYYMMDD; MMDD takes the year nearest the reference."""
    if len(token) == 8:
        return int(token[:4]), int(token[4:6]), int(token[6:])
    if len(token) == 6:
        return 2000 + int(token[:2]), int(token[2:4]), int(token[4:])
    month, day = int(token[:2]), int(token[2:])
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(datetime(year, month, day))
        except ValueError:  # Feb 29 outside a leap year
            continue
    best = min(candidates, key=lambda c: abs(c - reference.replace(tzinfo=None)))
    return best.year, best.month, best.day


def _parse_dc(token: str, tz) -> datetime:
    digits = token[2:]
    if len(digits) == 10:
        digits = "20" + digits
    return datetime.strptime(digits[:12], "%Y%m%d%H%M").replace(tzinfo=tz)


def _interval(token: str) -> timedelta:
    unit, amount = token[2], int(token[3:].lstrip("+"))
    return {"N": timedelta(minutes=amount), "H": timedelta(hours=amount), "D": timedelta(days=amount)}[unit]


def _values(fields: list[str]) -> list[float | None]:
    if fields and not fields[0].strip():
        fields = fields[1:]
    if fields and not fields[-1].strip():
        fields = fields[:-1]
    out: list[float | None] = []
    for f in fields:
        f = f.strip()
        try:
            out.append(float(f))
        except ValueError:
            out.append(None)  # M / MM / blank = missing, time still advances
    return out


def parse_e_messages(product: str) -> list[EMessage]:
    messages = []
    lines = product.splitlines()
    i = 0
    while i < len(lines):
        m = _HEADER.match(lines[i].strip())
        if not m:
            i += 1
            continue
        lid, date_token, tz_code, rest = m.groups()
        tz = _TZ.get(tz_code)
        fields = _strip_comments(rest).split("/")
        codes = {f.strip()[:2]: f.strip() for f in fields[:6] if f.strip()[:2] in ("DC", "DH", "DI")}
        pe_field = next((f.strip() for f in fields if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3,5}", f.strip())), None)
        if tz is None or "DC" not in codes or "DI" not in codes or pe_field is None:
            i += 1
            continue
        try:
            created = _parse_dc(codes["DC"], tz)
            year, month, day = _resolve_date(date_token, created)
            dh = codes.get("DH", "DH24")[2:]
            hour, minute = int(dh[:2]), int(dh[2:4] or 0)
            start = datetime(year, month, day, tzinfo=tz) + timedelta(hours=hour, minutes=minute)
            step = _interval(codes["DI"])
        except (ValueError, KeyError, IndexError):
            # Hand-typed bulletins occasionally carry impossible dates or codes; drop just that message.
            log.warning("skipping malformed SHEF .E message: %s", lines[i].strip())
            i += 1
            continue
        di_index = next(k for k, f in enumerate(fields) if f.strip().startswith("DI"))
        raw_values = _values([""] + fields[di_index + 1 :])
        i += 1
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith(":"):  # column headings like ":QPF FCST 7AM 1PM ..."
                i += 1
                continue
            c = _CONTINUATION.match(line)
            if not c:
                break
            raw_values += _values(_strip_comments(c.group(1)).split("/"))
            i += 1
        values = [
            ((start + k * step).astimezone(timezone.utc), v) for k, v in enumerate(raw_values) if v is not None
        ]
        messages.append(EMessage(lid, pe_field[:2], created.astimezone(timezone.utc), values))
    return messages


def normalize_product(product: str, pil: str, ctx: Context, fetched_at: datetime) -> tuple[datetime, pd.DataFrame] | None:
    messages = parse_e_messages(product)
    if not messages:
        return None
    issue_time = max(m.created for m in messages)
    rows = []
    for msg in messages:
        variable = _PE_VARIABLE.get(msg.pe)
        if variable is None or not msg.values:
            continue
        point = ctx.config.by_lid.get(msg.lid)
        usgs = point.usgs if point else None
        times = [t for t, _ in msg.values]
        vals = [v for _, v in msg.values]
        rows.append(pd.DataFrame({"location_id": msg.lid, "usgs_site": usgs, "variable": variable,
                                  "issue_time": msg.created, "valid_time": times, "value": vals, "qualifier": pil}))
        if variable == "stage_ft" and point and point.usgs and point.kind == "stage":
            try:
                r, _ = rating.fetch_rating(ctx, point.usgs)
            except Exception:
                log.warning("no rating for %s; stage only", point.usgs)
                continue
            rows.append(pd.DataFrame({"location_id": msg.lid, "usgs_site": usgs, "variable": "flow_cfs",
                                      "issue_time": msg.created, "valid_time": times,
                                      "value": r.stage_to_discharge(vals), "qualifier": rating.label(r)}))
    if not rows:
        return None
    df = pd.concat(rows, ignore_index=True).dropna(subset=["value"])
    df["dataset"] = "marfc_rvf"
    df["fetched_at"] = fetched_at
    return issue_time, df


def collect(ctx: Context) -> Iterator[Issuance]:
    cursor = ctx.state.cursor("marfc_rvf")
    start = cursor - OVERLAP if cursor else (ctx.backfill_start or BACKFILL_START)
    end = ctx.now + timedelta(days=1)
    chunks = 0
    while start < end and chunks < MAX_CHUNKS_PER_RUN:
        chunk_end = min(start + CHUNK, end)
        for pil in ctx.config.rvf_pils:
            params = {"pil": pil, "fmt": "text", "limit": 9999,
                      "sdate": start.strftime("%Y-%m-%d"), "edate": chunk_end.strftime("%Y-%m-%d")}
            resp, raw = ctx.get(URL, params=params, timeout=180)
            for product in split_products(resp.text):
                parsed = normalize_product(product, pil, ctx, raw.fetched_at)
                if parsed is None:
                    continue
                issue_time, frame = parsed
                digest = hashlib.sha256(product.encode()).hexdigest()[:8]
                key = f"{pil}/{issue_time.isoformat()}/{digest}"
                if ctx.state.has("marfc_rvf", key):
                    continue
                payload = RawPayload(raw.url, raw.fetched_at, raw.status, "text/plain", product.encode())
                yield Issuance("marfc_rvf", key, issue_time, frame, [payload])
        # Advance even through empty stretches so a gap in the archive can't stall the backfill.
        ctx.state.advance_cursor("marfc_rvf", min(chunk_end, ctx.now) - OVERLAP)
        start = chunk_end
        chunks += 1
