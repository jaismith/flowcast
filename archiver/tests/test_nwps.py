import pandas as pd

from flowcast_archiver.sources import nwps
from conftest import FETCHED_AT, fixture_json


def test_stageflow_gauge_has_stage_and_flow_in_cfs():
    body = fixture_json("nwps_stageflow_CCRN6.json")
    df = nwps.normalize_stageflow_forecast(body, "CCRN6", "01427510", "stage", FETCHED_AT)
    n = len(body["data"])
    assert (df["variable"] == "stage_ft").sum() == n
    assert (df["variable"] == "flow_cfs").sum() == n
    first = df[df["valid_time"] == pd.Timestamp("2026-09-26T00:00:00Z")].set_index("variable")["value"]
    assert first["stage_ft"] == 3.4
    assert first["flow_cfs"] == 1320.0  # 1.32 kcfs
    assert (df["issue_time"] == pd.Timestamp("2026-09-25T19:16:00Z")).all()
    assert set(df["qualifier"]) == {"HGIFF"}


def test_stageflow_reservoir_is_pool_and_drops_missing_flow():
    body = fixture_json("nwps_stageflow_CNNN6.json")
    df = nwps.normalize_stageflow_forecast(body, "CNNN6", None, "pool", FETCHED_AT)
    assert set(df["variable"]) == {"pool_elev_ft"}
    assert df["value"].iloc[0] == 1132.5


def test_short_range_is_one_deterministic_series():
    body = fixture_json("nwps_reach_short_range.json")
    [(dataset, ref, df)] = nwps.normalize_reach_series(body, "short_range", "2617456", "01427510", FETCHED_AT)
    assert dataset == "nwm_short_range"
    assert ref == pd.Timestamp("2026-09-25T23:00:00Z")
    assert df["member"].isna().all()
    assert len(df) == len(body["shortRange"]["series"]["data"])
    assert set(df["variable"]) == {"flow_cfs"}


def test_medium_range_keeps_mean_and_members():
    body = fixture_json("nwps_reach_medium_range.json")
    [(dataset, ref, df)] = nwps.normalize_reach_series(body, "medium_range", "2617456", "01427510", FETCHED_AT)
    assert dataset == "nwm_medium_range"
    assert ref == pd.Timestamp("2026-09-25T18:00:00Z")
    assert len(df) == 18
    assert set(df.loc[df["qualifier"] == "mean", "member"].isna()) == {True}
    assert sorted(df["member"].dropna().unique()) == [1, 2]


def test_empty_series_yields_nothing():
    body = fixture_json("nwps_reach_medium_range.json")
    assert nwps.normalize_reach_series(body, "short_range", "2617456", None, FETCHED_AT) == []
