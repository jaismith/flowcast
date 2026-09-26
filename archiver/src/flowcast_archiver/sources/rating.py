"""USGS stage-discharge ratings (expanded, shift-adjusted "EXSA" tables).

Fetched from the USGS Water Data STAC `ratings` collection through the pipeline's
`WaterDataClient` (the legacy NWISWeb `get_ratings` endpoint is throttled from Nov 16,
2026 and shut down on Feb 22, 2027). Used to convert RVF stage forecasts to flow, and
archived whenever a site's rating or shift changes so past forecasts can later be
converted with the rating that was in force at the time.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from flowcast_pipeline.usgs import RatingCurve, ResponseCache, WaterDataClient, site_number
from flowcast_pipeline.usgs.client import STAC_BASE

from ..context import Context
from ..model import Issuance, RawPayload
from ..store import utcnow

log = logging.getLogger(__name__)

# Lambda can only write under /tmp; a warm container reuses ratings for a few hours.
CACHE_DIR = Path(tempfile.gettempdir()) / "flowcast-usgs"
CACHE_TTL = timedelta(hours=6)


def label(curve: RatingCurve) -> str:
    return f"usgs_rating:{site_number(curve.site_id)}:{curve.rating_id or 'unknown'}"


def _client(ctx: Context) -> WaterDataClient:
    if "usgs_client" not in ctx.cache:
        ctx.cache["usgs_client"] = WaterDataClient(
            cache=ResponseCache(CACHE_DIR), session=ctx.session, max_retries=3, timeout_s=60
        )
    return ctx.cache["usgs_client"]


def fetch_rating(ctx: Context, site: str) -> tuple[RatingCurve, RawPayload]:
    cache = ctx.cache.setdefault("ratings", {})
    if site not in cache:
        fetched_at = utcnow()
        try:
            text = _client(ctx).rating_rdb(site, "exsa", ttl=CACHE_TTL)
            url = f"{STAC_BASE}/collections/ratings/items/USGS-{site_number(site)}.exsa.rdb"
            raw = RawPayload(url, fetched_at, 200, "text/plain", text.encode())
            cache[site] = (RatingCurve.from_rdb(site, "exsa", text), raw)
        except Exception as exc:
            # Remembered for the rest of the run: RVF conversion asks once per bulletin.
            cache[site] = exc
    if isinstance(cache[site], Exception):
        raise cache[site]
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
        # Raw RDB text only; RatingCurve.from_rdb() reads it back.
        yield Issuance("usgs_rating", key, raw.fetched_at, None, [raw])
