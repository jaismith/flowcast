from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from flowcast_damsched import validate
from flowcast_damsched.fetch import Fetched
from flowcast_damsched.schema import Kind, hourly
from flowcast_damsched.sources import lcra, release_calendar, safewaters, swpa

FIX = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 9, 29, 1, 20, tzinfo=UTC)


def fetched(name: str) -> Fetched:
    return Fetched(f"file://{name}", (FIX / name).read_bytes(), FETCHED_AT, 200, "")


# SWPA

def test_swpa_uses_body_date_not_title():
    long, table, meta = swpa.parse((FIX / "swpa_tue.htm").read_text())
    assert meta["date"] == date(2026, 9, 22)
    assert meta["title_date"] == date(2026, 9, 28)
    assert len(table) == 18 and len(long) == 18 * 24


def test_swpa_hours_are_hour_ending_central():
    long, table, _ = swpa.parse((FIX / "swpa_mon.htm").read_text())
    bs = long[long["dam"] == "Bull Shoals"].sort_values("valid_start")
    assert str(bs["valid_start"].iloc[0]) == "2026-09-28 00:00:00-05:00"
    assert str(bs["valid_end"].iloc[-1]) == "2026-09-29 00:00:00-05:00"
    # The TOT row matches the sum of the hourly values.
    assert bs["value"].sum() == 501
    assert "Webbers Falls L&D" in set(table["project"])


def test_swpa_rejects_a_changed_layout():
    page = (FIX / "swpa_mon.htm").read_text().replace("PROJECTED LOADING SCHEDULE", "LOADING PLAN")
    with pytest.raises(swpa.ScheduleFormatError):
        swpa.parse(page)


def test_swpa_nameplate_conversion():
    assert swpa.mw_to_cfs(pd.Series([391.0]), 391.0, 26400.0).iloc[0] == 26400.0


# Safe Waters

def test_safewaters_interval_schedule_in_eastern_time():
    got = fetched("safewaters_fife-brook.html")
    rows, meta = safewaters.parse_facility(got.text, "fife-brook", got)
    sched = [r for r in rows if r["kind"] == Kind.SCHEDULED_RELEASE]
    assert [r["value"] for r in sched] == [125, 800, 125, 900, 125]
    assert str(sched[1]["valid_start"]) == "2026-09-28 17:00:00-04:00"
    # "11:59 PM" runs through midnight.
    assert str(sched[-1]["valid_end"]) == "2026-09-30 00:00:00-04:00"
    assert any(f.endswith(".pdf") for f in meta["long_term_files"])
    assert meta["now"][0][:2] == (132.8, "cfs")


def test_safewaters_station_column_and_daily_tables():
    got = fetched("safewaters_moosehead-east-west-outlet.html")
    rows, _ = safewaters.parse_facility(got.text, "moosehead-east-west-outlet", got)
    assert {r["dam"] for r in rows} == {"moosehead-east-west-outlet:east-outlet", "moosehead-east-west-outlet:west-outlet"}
    got = fetched("safewaters_milford.html")
    rows, _ = safewaters.parse_facility(got.text, "milford", got)
    assert rows[0]["value"] == 3048 and rows[0]["unit"] == "cfs_daily_mean"


def test_safewaters_unknown_layout_becomes_notice():
    got = fetched("safewaters_calderwood.html")
    rows, _ = safewaters.parse_facility(got.text, "calderwood", got)
    assert [r["kind"] for r in rows] == [Kind.NOTICE]
    assert "Generators Running" in rows[0]["note"]


def test_safewaters_embedded_observed_values():
    got = fetched("safewaters_fife-brook.html")
    obs = safewaters.parse_observed(got.text, got)
    flows = {r["dam"]: r["value"] for r in obs if r["kind"] == Kind.OBSERVED_RELEASE}
    assert flows["fife-brook"] == "132.80"
    assert len(flows) > 30
    assert len(safewaters.facility_coords(got.text)) > 40


# PDF calendars

def test_fife_brook_calendar_days():
    days = release_calendar.parse_pdf((FIX / "release_calendar_fife-brook_2026.pdf").read_bytes())
    june = {d.day for d in days["date"] if d.month == 6}
    assert june == {13, 14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28}
    recs = release_calendar.to_records(days, release_calendar.FIFE_BROOK_2026, fetched("release_calendar_fife-brook_2026.pdf"))
    june27 = recs[recs["valid_start"].dt.tz_convert("America/New_York").dt.date == date(2026, 6, 27)]
    assert june27["valid_start"].dt.tz_convert("America/New_York").dt.hour.tolist() == [10]
    assert (recs["value"] == 800).all()


def test_calendar_legend_text_comes_with_the_color():
    days = release_calendar.parse_pdf((FIX / "release_calendar_rapid-magalloway_2026.pdf").read_bytes())
    legends = set(days["legend"])
    assert any("1200 cfs" in text for text in legends)
    assert any("Rapid River Release of 1800 cfs" in text for text in legends)
    assert len(days) == 22


# LCRA

def test_lcra_discharge_and_notices():
    got = fetched("lcra_discharge.json")
    df = lcra.parse_discharge(got.json(), got)
    assert set(df["dam"]) == set(lcra.DAMS.values())
    assert len(df) == 48 * 6
    got = fetched("lcra_gate_ops.json")
    ops = lcra.parse_gate_ops(got.json(), got)
    assert (ops["kind"] == Kind.NOTICE).sum() == 7


def test_lcra_missing_field_fails_loudly():
    got = fetched("lcra_discharge.json")
    body = got.json()
    for r in body["records"]:
        r.pop("travisDischarge")
    with pytest.raises(ValueError, match="travisDischarge"):
        lcra.parse_discharge(body, got)


# Validation

def test_hourly_check_recovers_lag_and_beats_persistence():
    idx = pd.date_range("2026-09-20", periods=96, freq="h", tz="UTC")
    sched = pd.Series(np.where((idx.hour >= 14) & (idx.hour < 20), 5000.0, 500.0), index=idx)
    gauge = sched.shift(2).fillna(500.0) + 100.0
    res = validate.hourly_check(sched, gauge, "America/Chicago")
    assert res.lag_h == 2
    assert res.r > 0.99
    assert res.mae_persist_delta < res.mae_persistence


def test_hourly_expands_intervals():
    df = pd.DataFrame({"source": "x", "dam": "d", "kind": Kind.SCHEDULED_RELEASE,
                       "valid_start": [pd.Timestamp("2026-09-28 00:00", tz="UTC")],
                       "valid_end": [pd.Timestamp("2026-09-28 03:00", tz="UTC")], "value": [10.0], "unit": "cfs",
                       "issue_time": FETCHED_AT, "fetched_at": FETCHED_AT, "note": None})
    assert hourly(df, "d", Kind.SCHEDULED_RELEASE).tolist() == [10.0, 10.0, 10.0]
