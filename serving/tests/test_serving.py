import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import pytest
from jsonschema import Draft202012Validator
from moto import mock_aws
from referencing import Registry, Resource

from flowcast_serve import api, config, control, eligibility, forecast, issues, names
from flowcast_serve.calibration import _bracket, calibrate_temperature_hourly
from flowcast_serve.registry import ServedSite, resolve

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def site(slug="beaverkill", pinned=False, usgs="01420500"):
    return ServedSite(usgs, slug, "Beaver Kill at Cooks Falls, NY", "Cooks Falls", 41.95, -74.98, "America/New_York", pinned, True, None)


# ---------------------------------------------------------------------------------------------- issue times


def test_latest_issue_waits_for_inputs():
    assert issues.latest_issue(datetime(2026, 10, 4, 13, 29, tzinfo=UTC)) == datetime(2026, 10, 4, 6, tzinfo=UTC)
    assert issues.latest_issue(datetime(2026, 10, 4, 13, 30, tzinfo=UTC)) == datetime(2026, 10, 4, 12, tzinfo=UTC)
    assert issues.latest_issue(datetime(2026, 10, 5, 1, 0, tzinfo=UTC)) == datetime(2026, 10, 4, 18, tzinfo=UTC)


def test_live_issues_never_reach_back_into_the_frozen_test_years():
    with pytest.raises(ValueError):
        issues.check_live(datetime(2026, 9, 30, 18, tzinfo=UTC))
    with pytest.raises(ValueError):
        issues.check_live(datetime(2026, 10, 4, 3, tzinfo=UTC))
    issues.check_live(datetime(2026, 10, 4, 6, tzinfo=UTC))


# ---------------------------------------------------------------------------------------------- control table


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="eu-west-1")
        ddb.create_table(TableName="flowcast-control", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        yield control.Control("flowcast-control", ddb)


def test_visit_wakes_for_seven_days_then_snoozes(table):
    now = datetime(2026, 10, 4, 15, 10, tzinfo=UTC)
    s = site()
    assert control.status(s, table.site(s.site_id), None, now).status == "snoozed"
    item, capped = table.record_visit(s, now)
    assert not capped and item["awake_until"] == issues.iso(now + timedelta(days=7))
    assert control.is_active(s, item, now + timedelta(days=6))
    assert not control.is_active(s, item, now + timedelta(days=7, minutes=1))
    assert control.status(s, item, None, now + timedelta(days=8)).status == "snoozed"


def test_three_visit_days_in_two_weeks_keep_a_site_active_for_thirty_days(table):
    s = site()
    t0 = datetime(2026, 10, 4, 12, tzinfo=UTC)
    for d in (0, 3, 6):
        item, _ = table.record_visit(s, t0 + timedelta(days=d))
    assert item["awake_until"] == issues.iso(t0 + timedelta(days=6 + config.POPULAR_DAYS))


def test_visit_cap_leaves_new_sites_paused(table):
    item, capped = table.record_visit(site(), datetime(2026, 10, 4, tzinfo=UTC), visit_active_count=config.VISIT_ACTIVE_CAP)
    assert capped and "awake_until" not in item
    assert control.status(site(), item, None, datetime(2026, 10, 4, tzinfo=UTC), capped=True).status == "paused"


def test_pinned_and_alert_sites_are_always_on(table):
    pinned = site("callicoon", pinned=True)
    assert control.status(pinned, {}, None, datetime(2026, 10, 4, tzinfo=UTC)).always_on
    s = site()
    assert table.set_alerts(s.site_id, +1) == 1
    assert control.always_on(s, table.site(s.site_id)) == ["alerts:1"]
    assert table.set_alerts(s.site_id, -1) == 0 and table.set_alerts(s.site_id, -1) == 0


def test_a_run_lock_collapses_a_wake_and_a_cycle(table):
    now = datetime(2026, 10, 4, 13, 31, tzinfo=UTC)
    assert table.acquire("USGS-01420500", "2026100412", "v1", "wake", now)
    assert not table.acquire("USGS-01420500", "2026100412", "v1", "cycle", now)
    assert table.acquire("USGS-01420500", "2026100412", "v1", "cycle", now + timedelta(minutes=config.RUN_LEASE_MIN + 1))
    table.finish("USGS-01420500", "2026100412", "v1", False, now)
    assert table.acquire("USGS-01420500", "2026100412", "v1", "cycle", now)  # failed with attempts < 3: retry
    table.finish("USGS-01420500", "2026100412", "v1", True, now)
    assert not table.acquire("USGS-01420500", "2026100412", "v1", "cycle", now)
    assert table.site("USGS-01420500")["last_issue"] == "2026100412"


def test_waking_and_delayed_states(table):
    now = datetime(2026, 10, 4, 15, 10, tzinfo=UTC)
    s = site("callicoon", pinned=True)
    table.acquire(s.site_id, "2026100412", "v1", "wake", now)
    assert control.status(s, table.site(s.site_id), table.latest_run(s.site_id), now).status == "waking"
    table.finish(s.site_id, "2026100412", "v1", True, now)
    assert control.status(s, table.site(s.site_id), table.latest_run(s.site_id), now).status == "active"
    assert control.status(s, table.site(s.site_id), table.latest_run(s.site_id), now + timedelta(hours=8)).status == "delayed"


# ---------------------------------------------------------------------------------------------- API


def test_api_refuses_unknown_and_unsupported_sites(monkeypatch):
    monkeypatch.setattr(api, "resolve", lambda raw: None)
    unsupported = api.handler({"rawPath": "/api/visit", "queryStringParameters": {"site": "USGS-09380000"}, "requestContext": {"http": {"method": "POST"}}}, None)
    assert unsupported["statusCode"] == 404 and json.loads(unsupported["body"])["error"] == "not_supported"
    unknown = api.handler({"rawPath": "/api/status", "queryStringParameters": {"site": "nope"}}, None)
    assert json.loads(unknown["body"])["error"] == "unknown_site"


def test_resolve_takes_ids_and_slugs():
    sites = {s.site_id: s for s in (site(), site("callicoon", pinned=True, usgs="01427510"))}
    assert resolve("USGS-01420500", sites).slug == "beaverkill"
    assert resolve("01420500", sites).slug == "beaverkill"
    assert resolve("BeaverKill", sites) is not None and resolve("USGS-99999999", sites) is None


# ---------------------------------------------------------------------------------------------- names, eligibility


@pytest.mark.parametrize("raw,state,expected", [
    ("DELAWARE RIVER AT CALLICOON NY", "PA", ("Delaware River at Callicoon, NY", "Callicoon", "NY")),
    ("Allagash River near Allagash, Maine", "ME", ("Allagash River near Allagash, ME", "Allagash", "ME")),
    ("S F SHENANDOAH RIVER NEAR LURAY, VA", "VA", ("South Fork Shenandoah River near Luray, VA", "Luray", "VA")),
    ("TUG FORK AT WELCH, W. VA.", "WV", ("Tug Fork at Welch, WV", "Welch", "WV")),
    ("MILL RIVER AT SPRING STREET AT TAUNTON, MA", "MA", ("Mill River at Spring Street at Taunton, MA", "Taunton", "MA")),
    ("Mad River at West Liberty OH - 03266560", "OH", ("Mad River at West Liberty, OH", "West Liberty", "OH")),
])
def test_station_names(raw, state, expected):
    n = names.parse(raw, state)
    assert (n.name, n.town, n.state) == expected


def test_eligibility_rule():
    now = pd.Timestamp("2026-10-04", tz="UTC")
    ids = ["USGS-A", "USGS-B", "USGS-C", "USGS-D", "USGS-E", "USGS-01427510", "USGS-01421000"]
    locs = pd.DataFrame({"monitoring_location_name": ids, "site_type_code": ["ST", "LK", "ST", "ST", "ST", "ST", "ST"],
                         "drainage_area": [100.0, 100.0, 5.0, 100.0, 100.0, 1820.0, 783.0], "hydrologic_unit_code": ["020401", "020401", "020401", "020401", "100100", "020401", "020401"],
                         "lat": 41.0, "lon": -75.0}, index=ids)
    iv = pd.DataFrame({"begin": pd.Timestamp("1990-01-01", tz="UTC"), "end": now}, index=ids)
    iv.loc["USGS-D", "begin"] = pd.Timestamp("2022-01-01", tz="UTC")
    iv.loc["USGS-01421000", "end"] = pd.Timestamp("2025-12-08", tz="UTC")
    tw = iv.loc[["USGS-A"]]
    t = eligibility.evaluate(iv, iv.iloc[0:0], tw, locs, {"01427510", "01421000"}, now)
    assert t.loc["USGS-A", "status"] == "eligible" and t.loc["USGS-A", "has_temp"] and t.loc["USGS-A", "has_q"]
    assert t.loc["USGS-B", "reasons"] == ["not_a_stream"]
    assert t.loc["USGS-C", "reasons"] == ["area_out_of_range"]
    assert t.loc["USGS-D", "reasons"] == ["short_record"]
    assert t.loc["USGS-E", "reasons"] == ["outside_training_region"]
    assert t.loc["USGS-01427510", "status"] == "model_basin"
    assert t.loc["USGS-01421000", "status"] == "ineligible" and t.loc["USGS-01421000", "reasons"] == ["no_recent_discharge"]
    entries = eligibility.index_flags(t, [{"id": "USGS-01427510"}, {"id": "USGS-01421000"}])
    assert [(e["forecastable"], e["not_forecastable_reason"]) for e in entries] == [(True, None), (False, "no_recent_discharge")]


# ---------------------------------------------------------------------------------------------- calibration, contract


def test_lead_bracketing_and_temperature_offsets():
    fitted = np.array([1.0, 2.0, 3.0, 6.0, 168.0])
    assert _bracket(fitted, 4.0) == (3.0, 6.0, pytest.approx(1 / 3))
    assert _bracket(fitted, 180.0) == (168.0, 168.0, 0.0)
    cal = pd.DataFrame({"variable": "water_temperature", "wy": 0, "lead_h": [1.0, 3.0], "offset": [0.0, 0.2], "scale": [1.0, 2.0]})
    x = np.tile(np.array([[10.0, 11.0, 12.0]]), (3, 1))
    out = calibrate_temperature_hourly(x, cal)
    assert out[1].tolist() == pytest.approx([9.6, 11.1, 12.6])


def test_examples_follow_the_contract():
    schemas = {p.name: json.loads(p.read_text()) for p in (ROOT / "schema").glob("*.json")}
    reg = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    ex = ROOT / "examples"
    site_dir = ex / "USGS-01427510"
    for schema, doc in ((schemas["sites.schema.json"], ex / "sites.json"), (schemas["live.schema.json"], site_dir / "live.json"),
                        (schemas["static.schema.json"], site_dir / "static.json"), *((schemas["forecast.schema.json"], f) for f in (site_dir / "forecasts").glob("*.json"))):
        errors = list(Draft202012Validator(schema, registry=reg).iter_errors(json.loads(doc.read_text())))
        assert not errors, (doc.name, [e.message for e in errors[:3]])
    gauges = schemas["gauges.schema.json"]["$id"]
    for part, doc in (("index", ex / "gauges" / "index.json"), ("tile", ex / "gauges" / "tiles" / "-80_40.json")):
        assert not list(Draft202012Validator({"$ref": f"{gauges}#/$defs/{part}"}, registry=reg).iter_errors(json.loads(doc.read_text())))


def test_forecast_runs_refuse_issues_whose_inputs_have_not_landed():
    with pytest.raises(ValueError, match="not available yet"):
        forecast.run(["USGS-01427510"], issues.issue_key(issues.latest_issue() + timedelta(hours=6)), "manual", True, config.Settings())
