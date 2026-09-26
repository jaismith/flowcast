import json

import pandas as pd
import pytest
import responses

from flowcast_pipeline.ingest import ingest, ingest_health
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.obs import obs_series, read_obs
from flowcast_pipeline.usgs import ResponseCache, WaterDataClient
from flowcast_pipeline.usgs.client import OGC_BASE

CONTINUOUS_URL = f"{OGC_BASE}/collections/continuous/items"
NOW = pd.Timestamp("2026-09-26T03:10Z")


def feature(site, time, value):
    props = {"monitoring_location_id": site, "time": time, "value": str(value), "approval_status": "Provisional", "qualifier": None, "time_series_id": f"ts-{site}"}
    return {"type": "Feature", "id": f"{site}-{time}", "geometry": None, "properties": props}


def page(features):
    return {"type": "FeatureCollection", "features": features, "links": [], "numberReturned": len(features)}


@pytest.fixture
def client(tmp_path):
    return WaterDataClient(api_key="", cache=ResponseCache(tmp_path / "cache"), backoff_s=0.0, max_retries=0)


def discharge_page():
    return page(
        [
            feature("USGS-01427510", "2026-09-26T01:00:00+00:00", 1300),
            feature("USGS-01427510", "2026-09-26T01:15:00+00:00", 1310),
            feature("USGS-01427510", "2026-09-26T02:00:00+00:00", 1290),
            feature("USGS-01425000", "2026-09-26T02:00:00+00:00", 250),
        ]
    )


@responses.activate
def test_continuous_many_queries_all_sites_at_once(client):
    responses.get(CONTINUOUS_URL, json=discharge_page())
    df = client.continuous_many(["01427510", "USGS-01425000"], "00060", NOW - pd.Timedelta(hours=3), NOW)
    query = responses.calls[0].request.params
    assert query["monitoring_location_id"] == "USGS-01427510,USGS-01425000"
    assert query["time"] == "2026-09-26T00:10:00Z/2026-09-26T03:10:00Z"
    assert df["monitoring_location_id"].tolist() == ["USGS-01425000", *["USGS-01427510"] * 3]
    assert df["value"].tolist() == [250.0, 1300.0, 1310.0, 1290.0]


@responses.activate
def test_ingest_writes_hourly_obs_and_an_ok_marker(tmp_path, client):
    responses.get(CONTINUOUS_URL, json=discharge_page())
    responses.get(CONTINUOUS_URL, json=page([]))
    responses.get(CONTINUOUS_URL, status=500)
    lake = Lake(tmp_path / "lake")

    report = ingest(lake, ["USGS-01427510", "USGS-01425000"], client, now=NOW)

    assert report.rows == {"discharge": 3, "water_temperature": 0}
    assert report.failed == ["stage"]
    q = obs_series(lake, "01427510", "discharge")
    assert q.to_dict() == {pd.Timestamp("2026-09-26T01:00Z"): 1300.0, pd.Timestamp("2026-09-26T02:00Z"): 1290.0}
    assert read_obs(lake, "01425000", "discharge")["value"].tolist() == [250.0]
    markers = lake.list("_runs/obs-ingest/")
    assert markers == ["_runs/obs-ingest/2026-09-26/20260926T031000Z-failed.json"]
    assert json.loads(lake.read(markers[0]))["failed"] == ["stage"]


def test_ingest_health_counts_missed_cycles(tmp_path):
    lake = Lake(tmp_path)
    start = pd.Timestamp("2026-09-20T00:10Z")
    for k in range(48):
        if k in (5, 30):
            continue
        t = start + pd.Timedelta(hours=k)
        lake.write(f"_runs/obs-ingest/{t:%Y-%m-%d}/{t:%Y%m%dT%H%M%SZ}-ok.json", b"{}")
    lake.write("_runs/obs-ingest/2026-09-21/20260921T063000Z-failed.json", b"{}")

    health = ingest_health(lake, now=pd.Timestamp("2026-09-22T00:30Z"))

    assert health["first_run"] == "2026-09-20T00:00:00+00:00"
    assert health["expected_cycles"] == 48
    assert health["missed_cycles"] == 2
    assert health["failed_runs"] == 1
    assert health["missed_fraction"] == pytest.approx(2 / 48)
