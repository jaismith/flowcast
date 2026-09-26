"""USGS stage-discharge ratings (expanded, shift-adjusted "EXSA" tables).

Used to convert RVF stage forecasts to flow, and archived whenever a site's
rating or shift changes so past forecasts can later be converted with the
rating that was in force at the time.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from ..context import Context
from ..model import Issuance, RawPayload

log = logging.getLogger(__name__)

URL = "https://waterdata.usgs.gov/nwisweb/get_ratings"


@dataclass
class Rating:
    site: str
    rating_id: str
    shifted_at: str
    stage: np.ndarray
    flow: np.ndarray

    @property
    def label(self) -> str:
        return f"usgs_rating:{self.site}:{self.rating_id}"

    def to_flow(self, stage) -> np.ndarray:
        stage = np.asarray(stage, dtype="float64")
        flow = np.interp(stage, self.stage, self.flow)
        return np.where((stage < self.stage[0]) | (stage > self.stage[-1]), np.nan, flow)


def parse_exsa(text: str, site: str) -> Rating:
    rating_id = re.search(r'RATING ID="([^"]+)"', text)
    shifted = re.search(r'RATING SHIFTED="([^"]+)"', text)
    rows = []
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    for line in lines[2:]:  # column names, then column formats
        parts = line.split("\t")
        rows.append((float(parts[0]), float(parts[2])))
    table = np.array(rows)
    return Rating(
        site=site,
        rating_id=rating_id.group(1) if rating_id else "unknown",
        shifted_at=shifted.group(1) if shifted else "",
        stage=table[:, 0],
        flow=table[:, 1],
    )


def fetch_rating(ctx: Context, site: str) -> tuple[Rating, RawPayload]:
    cache = ctx.cache.setdefault("ratings", {})
    if site not in cache:
        resp, raw = ctx.get(URL, params={"site_no": site, "file_type": "exsa"})
        cache[site] = (parse_exsa(resp.text, site), raw)
    return cache[site]


def content_hash(text: str) -> str:
    body = "\n".join(ln for ln in text.splitlines() if "RETRIEVED:" not in ln)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def collect(ctx: Context) -> Iterator[Issuance]:
    for point in ctx.config.points:
        if not point.usgs or point.kind != "stage":
            continue
        try:
            _, raw = fetch_rating(ctx, point.usgs)
        except Exception:
            ctx.fail("rating fetch failed for %s", point.usgs)
            continue
        key = f"{point.usgs}/{content_hash(raw.body.decode())}"
        if ctx.state.has("usgs_rating", key):
            continue
        # Raw EXSA text only; parse_exsa() reads it back.
        yield Issuance("usgs_rating", key, raw.fetched_at, None, [raw])
