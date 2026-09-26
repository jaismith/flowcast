"""Helpers shared by the release-schedule sources."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")
CENTRAL = ZoneInfo("America/Chicago")


def digest(data: bytes | str, n: int = 16) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:n]


def local_midnight_utc(day: date, tz: ZoneInfo = EASTERN) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(), tzinfo=tz)).tz_convert("UTC")


def number(text) -> float | None:
    """Parse '3,117', ' 275 ', '+150' or a float; None for blanks and non-numbers."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = str(text).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()
