import math

import pytest
import requests
from flowcast_pipeline.usgs import RatingCurve
from flowcast_pipeline.usgs.client import STAC_BASE

from flowcast_archiver.sources import rating
from conftest import fixture_json, fixture_text

ITEM = f"{STAC_BASE}/collections/ratings/items/USGS-01427510.exsa.rdb"
ASSET = "https://api.waterdata.usgs.gov/stac-files/ratings/USGS.01427510.exsa.rdb"


def curve() -> RatingCurve:
    return RatingCurve.from_rdb("01427510", "exsa", fixture_text("usgs_rating_01427510_exsa.rdb"))


def test_rdb_reads_id_and_table():
    r = curve()
    assert r.rating_id == "17.0"
    assert rating.label(r) == "usgs_rating:01427510:17.0"
    assert (r.stage_ft[0], r.discharge_cfs[0]) == (2.66, 295.0)
    assert len(r.stage_ft) == 300


def test_stage_to_discharge_interpolates_and_blanks_out_of_range():
    low, mid, high = curve().stage_to_discharge([2.0, 2.665, 99.0])
    assert math.isnan(low) and math.isnan(high)
    assert 295.0 < mid < 303.59


def test_rating_matches_nwps_flow_for_same_stage():
    point = fixture_json("nwps_stageflow_CCRN6.json")["data"][0]
    flow = curve().stage_to_discharge([point["primary"]])[0]
    assert abs(flow - point["secondary"] * 1000) < 0.02 * point["secondary"] * 1000


def test_content_hash_ignores_retrieval_timestamp():
    text = fixture_text("usgs_rating_01427510_exsa.rdb")
    refetched = text.replace("RETRIEVED: 2026-07-21 17:55:01", "RETRIEVED: 2026-09-26 02:00:00")
    assert refetched != text
    assert rating.content_hash(refetched) == rating.content_hash(text)
    assert rating.content_hash(text.replace("295.00", "296.00")) != rating.content_hash(text)


class StacSession(requests.Session):
    """Serves the STAC item and its RDB asset; anything else (e.g. legacy NWISWeb) is an error."""

    def __init__(self, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.urls: list[str] = []

    def get(self, url, *args, **kwargs):
        self.urls.append(url)
        resp = requests.Response()
        resp.url = url
        if self.fail:
            resp.status_code = 404
            resp._content = b"not found"
        elif url == ITEM:
            resp.status_code = 200
            resp._content = ('{"assets": {"data": {"href": "%s"}}}' % ASSET).encode()
        elif url == ASSET:
            resp.status_code = 200
            resp._content = fixture_text("usgs_rating_01427510_exsa.rdb").encode()
        else:
            raise AssertionError(f"unexpected request {url}")
        return resp


@pytest.fixture
def stac_ctx(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(rating, "CACHE_DIR", tmp_path)
    ctx.cache.clear()  # a fresh client, built on whichever session the test installs
    return ctx


def test_fetch_rating_uses_water_data_stac(stac_ctx):
    stac_ctx.session = StacSession()
    r, raw = rating.fetch_rating(stac_ctx, "01427510")
    assert stac_ctx.session.urls == [ITEM, ASSET]
    assert rating.label(r) == "usgs_rating:01427510:17.0"
    assert raw.url == ITEM and raw.body.decode() == fixture_text("usgs_rating_01427510_exsa.rdb")
    rating.fetch_rating(stac_ctx, "01427510")
    assert len(stac_ctx.session.urls) == 2  # memoized for the run


def test_rating_source_archives_raw_rdb_once(stac_ctx):
    stac_ctx.session = StacSession()
    [iss] = [i for i in rating.collect(stac_ctx) if i.key.startswith("01427510/")]
    assert iss.frame is None and iss.raw[0].url == ITEM


def test_failed_rating_is_not_retried_within_a_run(stac_ctx):
    stac_ctx.session = StacSession(fail=True)
    with pytest.raises(Exception):
        rating.fetch_rating(stac_ctx, "01427510")
    calls = len(stac_ctx.session.urls)
    with pytest.raises(Exception):
        rating.fetch_rating(stac_ctx, "01427510")
    assert len(stac_ctx.session.urls) == calls
