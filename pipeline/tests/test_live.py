"""Smoke tests against the real USGS Water Data API. Run with `pytest -m live`."""

import pandas as pd
import pytest

from flowcast_pipeline.usgs import Parameter, ResponseCache, Statistic, WaterDataClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    return WaterDataClient(cache=ResponseCache(tmp_path_factory.mktemp("usgs")))


def test_live_discharge_and_temperature(client):
    end = pd.Timestamp.now(tz="UTC")
    q = client.continuous("01427510", Parameter.DISCHARGE, end - pd.Timedelta(days=2), end, use_cache=False)
    t = client.continuous("01427510", Parameter.WATER_TEMPERATURE, end - pd.Timedelta(days=2), end, use_cache=False)
    assert len(q) > 100 and q["value"].gt(0).all()
    assert len(t) > 100


def test_live_daily_metadata_and_rating(client):
    daily = client.daily("01427510", Parameter.DISCHARGE, "2024-01-01", "2024-01-31", Statistic.MEAN)
    assert len(daily) == 31
    assert client.monitoring_location("01427510")["drainage_area"] == pytest.approx(1820.0)
    assert (client.time_series_metadata("01427510", Parameter.DISCHARGE)["parameter_code"] == "00060").all()
    assert client.rating("01427510").stage_to_discharge(9.0) > 20_000
