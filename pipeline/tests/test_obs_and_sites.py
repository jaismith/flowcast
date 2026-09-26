import pandas as pd

from flowcast_pipeline.obs import read_obs, to_hourly, write_obs
from flowcast_pipeline.sites import load_sites


def iv_frame(times, values):
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "value": values,
            "approval_status": "Approved",
            "qualifier": None,
            "time_series_id": "ts1",
        }
    )


def test_to_hourly_takes_top_of_hour_within_tolerance():
    iv = iv_frame(
        ["2026-01-01T00:00Z", "2026-01-01T00:15Z", "2026-01-01T01:10Z", "2026-01-01T03:00Z"],
        [1.0, 2.0, 3.0, 4.0],
    )
    hourly = to_hourly(iv).set_index("time")["value"]
    assert hourly[pd.Timestamp("2026-01-01T00:00Z")] == 1.0
    assert hourly[pd.Timestamp("2026-01-01T01:00Z")] == 3.0
    assert pd.isna(hourly[pd.Timestamp("2026-01-01T02:00Z")])
    assert hourly[pd.Timestamp("2026-01-01T03:00Z")] == 4.0


def test_write_obs_replaces_revised_rows(tmp_path):
    first = iv_frame(["2026-09-01T00:00Z", "2026-09-01T01:00Z"], [10.0, 11.0])
    revised = iv_frame(["2026-09-01T01:00Z", "2026-10-01T00:00Z"], [12.0, 13.0])
    write_obs(tmp_path, "01427510", "discharge", first.drop(columns="time_series_id"))
    paths = write_obs(tmp_path, "01427510", "discharge", revised.drop(columns="time_series_id"))

    assert sorted(p.name for p in paths) == ["2026-09.parquet", "2026-10.parquet"]
    obs = read_obs(tmp_path, "USGS-01427510", "discharge")
    assert obs["value"].tolist() == [10.0, 12.0, 13.0]


def test_registry_has_callicoon():
    site = load_sites()["USGS-01427510"]
    assert site.nws_lid == "CCRN6"
    assert site.nwm_reach == 2617456
    assert site.stage_thresholds_ft["action"] == 9.0
    assert "USGS-01425000" in site.regulation_gauges
