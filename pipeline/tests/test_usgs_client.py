from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import responses

from flowcast_pipeline.usgs import Parameter, ResponseCache, Statistic, WaterDataClient, WaterDataError
from flowcast_pipeline.usgs.client import OGC_BASE, STAC_BASE

FIXTURES = Path(__file__).parent / "fixtures"
CONTINUOUS_URL = f"{OGC_BASE}/collections/continuous/items"


def feature(time, value, status="Approved"):
    return {
        "type": "Feature",
        "id": f"id-{time}",
        "geometry": None,
        "properties": {"time": time, "value": str(value), "approval_status": status, "qualifier": None, "time_series_id": "ts1"},
    }


def page(features, next_href=None):
    links = [{"rel": "next", "href": next_href}] if next_href else []
    return {"type": "FeatureCollection", "features": features, "links": links, "numberReturned": len(features)}


@pytest.fixture
def client(tmp_path):
    return WaterDataClient(api_key="", cache=ResponseCache(tmp_path), backoff_s=0.0)


@responses.activate
def test_continuous_follows_next_links_and_parses(client):
    next_href = f"{CONTINUOUS_URL}?offset=2"
    responses.get(CONTINUOUS_URL, json=page([feature("2020-06-01T00:00:00+00:00", 1500), feature("2020-06-01T00:15:00+00:00", 1510)], next_href))
    responses.get(next_href, json=page([feature("2020-06-01T00:30:00+00:00", 1520)]))

    df = client.continuous("01427510", Parameter.DISCHARGE, "2020-06-01", "2020-06-02", use_cache=False)

    assert list(df["value"]) == [1500.0, 1510.0, 1520.0]
    assert str(df["time"].dt.tz) == "UTC"
    first = responses.calls[0].request
    assert "monitoring_location_id=USGS-01427510" in first.url
    assert "parameter_code=00060" in first.url
    assert "skipGeometry=true" in first.url


@responses.activate
def test_continuous_cache_is_permanent_for_approved_past_years(client):
    responses.get(CONTINUOUS_URL, json=page([feature("2020-06-01T00:00:00+00:00", 1500)]))

    first = client.continuous("01427510", "00060", "2020-06-01", "2020-06-30")
    second = client.continuous("01427510", "00060", "2020-06-01", "2020-06-30")

    assert len(responses.calls) == 1
    pd.testing.assert_frame_equal(first, second)
    assert "time=2020-01-01T00:00:00Z/2020-12-31T23:59:59Z" in responses.calls[0].request.url.replace("%2F", "/").replace("%3A", ":")


@responses.activate
def test_provisional_chunks_expire(tmp_path):
    client = WaterDataClient(api_key="", cache=ResponseCache(tmp_path), provisional_ttl=timedelta(0))
    responses.get(CONTINUOUS_URL, json=page([feature("2020-06-01T00:00:00+00:00", 1500, status="Provisional")]))

    client.continuous("01427510", "00060", "2020-06-01", "2020-06-30")
    client.continuous("01427510", "00060", "2020-06-01", "2020-06-30")

    assert len(responses.calls) == 2


@responses.activate
def test_multi_year_request_is_chunked_by_year(client):
    responses.get(CONTINUOUS_URL, json=page([]))
    client.continuous("01427510", "00060", "2018-12-31", "2020-01-02")
    assert len(responses.calls) == 3


@responses.activate
def test_retries_on_429_then_succeeds(client):
    responses.get(CONTINUOUS_URL, status=429, headers={"Retry-After": "0"})
    responses.get(CONTINUOUS_URL, json=page([feature("2020-06-01T00:00:00+00:00", 7)]))

    df = client.continuous("01427510", "00060", "2020-06-01", "2020-06-02", use_cache=False)

    assert df["value"].tolist() == [7.0]
    assert len(responses.calls) == 2


@responses.activate
def test_raises_after_client_error(client):
    responses.get(CONTINUOUS_URL, status=400, body="bad request")
    with pytest.raises(WaterDataError):
        client.continuous("01427510", "00060", "2020-06-01", "2020-06-02", use_cache=False)


@responses.activate
def test_api_key_header(tmp_path):
    client = WaterDataClient(api_key="secret", cache=ResponseCache(tmp_path))
    responses.get(CONTINUOUS_URL, json=page([]))
    client.continuous("01427510", "00060", "2020-06-01", "2020-06-02", use_cache=False)
    assert responses.calls[0].request.headers["X-Api-Key"] == "secret"


@responses.activate
def test_daily_values(client):
    responses.get(
        f"{OGC_BASE}/collections/daily/items",
        json=page([feature("2026-07-02", 28.0), feature("2026-07-01", 27.9)]),
    )
    df = client.daily("01427510", Parameter.WATER_TEMPERATURE, "2026-07-01", "2026-07-02", statistic=Statistic.MAXIMUM, use_cache=False)
    assert df["date"].tolist() == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]
    assert "statistic_id=00001" in responses.calls[0].request.url


@responses.activate
def test_monitoring_location_is_cached(client):
    url = f"{OGC_BASE}/collections/monitoring-locations/items/USGS-01427510"
    responses.get(url, json={"id": "USGS-01427510", "properties": {"drainage_area": 1820.0}, "geometry": {"type": "Point"}})
    assert client.monitoring_location("01427510")["drainage_area"] == 1820.0
    assert client.monitoring_location("USGS-01427510")["drainage_area"] == 1820.0
    assert len(responses.calls) == 1


@responses.activate
def test_rating_from_stac(client):
    rdb = (FIXTURES / "USGS.01427510.exsa.rdb").read_text()
    asset = "https://api.waterdata.usgs.gov/stac-files/ratings/USGS.01427510.exsa.rdb"
    responses.get(f"{STAC_BASE}/collections/ratings/items/USGS-01427510.exsa.rdb", json={"assets": {"data": {"href": asset}}})
    responses.get(asset, body=rdb)

    curve = client.rating("01427510")

    assert curve.rating_id == "17.0"
    assert curve.stage_to_discharge(2.66) == pytest.approx(295.0)
    assert curve.stage_to_discharge(23.0) == pytest.approx(177000.0)
    assert np.isnan(curve.stage_to_discharge(30.0))
    flows = np.array([1000.0, 20000.0, 56200.0])
    np.testing.assert_allclose(curve.stage_to_discharge(curve.discharge_to_stage(flows)), flows, rtol=1e-6)
    # The raw text comes from the same cache entry, without refetching.
    assert client.rating_rdb("01427510") == rdb
    assert len(responses.calls) == 2


@responses.activate
def test_continuous_parses_mixed_fractional_second_timestamps(client):
    responses.get(
        CONTINUOUS_URL,
        json=page([feature("2025-02-06T10:15:00+00:00", 10), feature("2025-02-06T10:28:34.865000+00:00", 11)]),
    )
    df = client.continuous("01427510", Parameter.DISCHARGE, "2025-02-06T10:00", "2025-02-06T11:00", use_cache=False)
    assert list(df["time"].dt.second) == [0, 34]
