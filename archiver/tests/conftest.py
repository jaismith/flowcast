from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from flowcast_pipeline.usgs import RatingCurve, ResponseCache, WaterDataClient

from flowcast_archiver.config import load_config
from flowcast_archiver.context import Context
from flowcast_archiver.model import RawPayload
from flowcast_archiver.store import State

FIXTURES = Path(__file__).with_name("fixtures")
FETCHED_AT = datetime(2026, 9, 26, 1, 40, tzinfo=timezone.utc)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str):
    return json.loads(fixture_text(name))


class OfflineSession(requests.Session):
    def get(self, url, *args, **kwargs):
        raise requests.ConnectionError(f"tests are offline: {url}")


@pytest.fixture
def ctx(tmp_path) -> Context:
    """A context with no network whose rating cache holds the Callicoon rating fixture."""
    session = OfflineSession()
    context = Context(load_config(), State(), session, FETCHED_AT)
    # No retries: other gauges' ratings fail fast instead of backing off.
    context.cache["usgs_client"] = WaterDataClient(cache=ResponseCache(tmp_path), session=session, max_retries=0)
    text = fixture_text("usgs_rating_01427510_exsa.rdb")
    raw = RawPayload("https://example.test/rating", FETCHED_AT, 200, "text/plain", text.encode())
    context.cache["ratings"] = {"01427510": (RatingCurve.from_rdb("01427510", "exsa", text), raw)}
    return context
