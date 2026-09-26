"""HEFS, USGS Delaware temperature, and old flowcast forecast parsers."""

import pandas as pd

from flowcast_archiver.sources import hefs, legacy, usgs_temp
from conftest import FETCHED_AT, FIXTURES, fixture_json


def test_hefs_members_are_trace_years_across_all_parameters():
    df = hefs.normalize_ensembles(fixture_json("hefs_ensembles_CCRN6.json"), "01427510", FETCHED_AT)
    assert len(df) == 4 * 2 * 4  # parameters x members x events in the fixture
    assert set(df["variable"]) == {"flow_cfs", "precip_in", "air_temp_f", "swe_in"}
    assert (df["issue_time"] == pd.Timestamp("2026-09-16T12:00:00Z")).all()
    assert df["member"].min() >= 1940
    flow = df[df["variable"] == "flow_cfs"].sort_values(["member", "valid_time"])
    assert flow["value"].iloc[0] == 1787.0704
    assert (flow["location_id"] == "CCRN6").all()


def test_hefs_drops_missing_values():
    groups = fixture_json("hefs_ensembles_CCRN6.json")
    member = groups[0][0]
    member["miss_val"] = -999.0
    member["events"][1]["value"] = -999.0
    member["events"][2]["value"] = None
    df = hefs.normalize_ensembles([[member]], None, FETCHED_AT)
    assert len(df) == 2


def test_usgs_temperature_workbook():
    content = (FIXTURES / "usgs_drb_temp_2026-09-25.xlsx").read_bytes()
    df = usgs_temp.parse_workbook(content, "2026-09-25", FETCHED_AT)
    assert set(df["qualifier"]) == {"0_cfs", "100_cfs"}  # release scenarios, one sheet each
    assert (df["issue_time"] == pd.Timestamp("2026-09-25T04:00:00Z")).all()  # local midnight
    lordville = df[(df["location_id"] == "DR @ Lordville") & (df["qualifier"] == "0_cfs")]
    assert (lordville["usgs_site"] == "01427207").all()
    tmax = lordville[lordville["variable"] == "water_temp_max_f"]
    assert sorted(tmax["quantile"].unique()) == [0.05, 0.5, 0.95]
    assert tmax["valid_time"].nunique() == 8  # issue day plus 7
    wide = tmax.pivot(index="valid_time", columns="quantile", values="value")
    assert (wide[0.05] <= wide[0.5]).all() and (wide[0.5] <= wide[0.95]).all()
    p75 = lordville[lordville["variable"] == "prob_water_temp_max_gt_75f"]
    assert p75["value"].between(0, 1).all()


def test_legacy_flowcast_forecast():
    df = legacy.normalize(fixture_json("flowcast_legacy_forecast.json"), FETCHED_AT)
    assert len(df) == 2 * 3 * 6  # variables x (median, 5th, 95th) x steps
    assert (df["issue_time"] == pd.Timestamp(1790380800, unit="s", tz="UTC")).all()
    temp = df[(df["variable"] == "water_temp_f") & df["quantile"].isna()]
    assert temp["value"].iloc[0] == 61.5161
    assert set(df["quantile"].dropna()) == {0.05, 0.95}
