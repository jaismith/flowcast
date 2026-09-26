import math

from flowcast_archiver.sources import rating
from conftest import fixture_json, fixture_text


def test_parse_exsa_reads_id_and_table():
    r = rating.parse_exsa(fixture_text("usgs_rating_01427510_exsa.rdb"), "01427510")
    assert r.rating_id == "17.0"
    assert r.label == "usgs_rating:01427510:17.0"
    assert r.shifted_at.startswith("20260721")
    assert (r.stage[0], r.flow[0]) == (2.66, 295.0)
    assert len(r.stage) == 300


def test_to_flow_interpolates_and_blanks_out_of_range():
    r = rating.parse_exsa(fixture_text("usgs_rating_01427510_exsa.rdb"), "01427510")
    low, mid, high = r.to_flow([2.0, 2.665, 99.0])
    assert math.isnan(low) and math.isnan(high)
    assert 295.0 < mid < 303.59


def test_rating_matches_nwps_flow_for_same_stage():
    r = rating.parse_exsa(fixture_text("usgs_rating_01427510_exsa.rdb"), "01427510")
    point = fixture_json("nwps_stageflow_CCRN6.json")["data"][0]
    assert abs(r.to_flow([point["primary"]])[0] - point["secondary"] * 1000) < 0.02 * point["secondary"] * 1000


def test_content_hash_ignores_retrieval_timestamp():
    text = fixture_text("usgs_rating_01427510_exsa.rdb")
    refetched = text.replace("RETRIEVED: 2026-07-21 17:55:01", "RETRIEVED: 2026-09-26 02:00:00")
    assert refetched != text
    assert rating.content_hash(refetched) == rating.content_hash(text)
    assert rating.content_hash(text.replace("295.00", "296.00")) != rating.content_hash(text)
