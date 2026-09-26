"""Office of the Delaware River Master daily data (Cannonsville, Pepacton, Neversink, Montague).

One zip per series (a single `Date,<quantity> <unit>` CSV), rolling over the last
30 days with a ~2-day lag. Nothing older is downloadable, so every changed zip is
archived: the FFMP Target Flow per reservoir, ODRM directed release, storage,
spill, diversion, the Montague flow target (a few days ahead) and the banks.
The issue time is when we first saw that version of the file.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from datetime import date
from urllib.parse import quote

import pandas as pd
import requests

from ..context import Context
from ..model import Issuance
from .common import digest, local_midnight_utc, slug

PAGE = "https://webapps.usgs.gov/ODRM/data/data.html"
BASE = "https://webapps.usgs.gov/ODRM/"
_ZIP = re.compile(r'href="[^"]*?(data-downloads/30day/[^"]+\.zip)"')

RESERVOIRS = ("Cannonsville", "Pepacton", "Neversink")
SITES = {"Montague": "01438500"}
# Longest first so "ft^3/s-day" wins over "ft^3/s".
UNITS = [
    ("ft^3/s-day", "cfs_day"),
    ("ft^3/s", "cfs"),
    ("ft^3", "ft3"),
    ("M US Gal/d", "mgd"),
    ("M US Gal", "mg"),
    ("ft", "ft"),
    ("in", "in"),
]


def series_names(page_html: str) -> list[str]:
    return sorted({m.rsplit("/", 1)[-1].removesuffix(".zip") for m in _ZIP.findall(page_html)})


def _unit(header: str) -> str:
    for suffix, name in UNITS:
        if header.endswith(" " + suffix):
            return name
    return slug(header)


def location_and_variable(series: str, header: str) -> tuple[str, str]:
    unit = _unit(header)
    first, _, rest = series.partition(" ")
    if first in RESERVOIRS or first in SITES:
        return first.lower(), f"{slug(rest)}_{unit}"
    return "odrm", f"{slug(series)}_{unit}"


def parse_csv(text: str, series: str, fetched_at) -> pd.DataFrame:
    lines = [ln.strip() for ln in text.replace("\r", "").splitlines() if ln.strip()]
    if not lines:
        return pd.DataFrame()
    header = lines[0].split(",", 1)[1].strip()
    location, variable = location_and_variable(series, header)
    rows = []
    for line in lines[1:]:
        day, _, value = line.partition(",")
        try:
            rows.append((local_midnight_utc(date.fromisoformat(day.strip())), float(value)))
        except ValueError:
            continue
    df = pd.DataFrame(rows, columns=["valid_time", "value"])
    df["dataset"] = "odrm"
    df["location_id"] = location
    df["usgs_site"] = SITES.get(series.partition(" ")[0])
    df["variable"] = variable
    df["qualifier"] = series
    df["issue_time"] = fetched_at
    df["fetched_at"] = fetched_at
    return df


def read_zip(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        [name] = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        return zf.read(name).decode("utf-8", errors="replace")


def collect(ctx: Context) -> Iterator[Issuance]:
    try:
        resp, _ = ctx.get(PAGE)
    except Exception:
        ctx.fail("ODRM data page failed")
        return
    names = series_names(resp.text)
    if not names:
        ctx.fail("ODRM data page lists no 30-day zips; the page layout may have changed")
        return
    for series in names:
        try:
            resp, raw = ctx.get(BASE + quote(f"data-downloads/30day/{series}.zip"))
        except requests.HTTPError as exc:
            # Some series (e.g. Neversink Spill) are linked but not published.
            ctx.warn("ODRM %s unavailable: %s", series, exc)
            continue
        except Exception:
            ctx.fail("ODRM %s failed", series)
            continue
        text = read_zip(raw.body)
        key = f"{series}/{digest(text)}"
        if ctx.state.has("odrm", key):
            continue
        frame = parse_csv(text, series, raw.fetched_at)
        yield Issuance("odrm", key, raw.fetched_at, frame if not frame.empty else None, [raw])
