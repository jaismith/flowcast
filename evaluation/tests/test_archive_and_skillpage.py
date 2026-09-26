import json

import numpy as np
import pandas as pd
import pytest

from flowcast_pipeline.lake import Lake
from flowcast_pipeline.sites import get_site
from flowcast_eval.archive import archive_summary, read_archive, to_forecasts
from flowcast_eval.baselines import Air2Stream
from flowcast_eval.schema import normalize_forecasts
from flowcast_eval.scoreboard import score_forecasts
from flowcast_eval.skillpage import MIN_VERIFIED_DAYS, build_payload, render_html, restore_cache, save_cache

ISSUE = pd.Timestamp("2026-09-20T13:45Z")


def archive_rows(dataset, variable, values, member=None, qualifier=None, usgs="01427510", location="CCRN6", issue=ISSUE, fetched="2026-09-20T14:20Z"):
    valid = pd.date_range(issue.ceil("6h"), periods=len(values), freq="6h")
    return pd.DataFrame(
        {
            "dataset": dataset, "location_id": location, "usgs_site": usgs, "variable": variable,
            "issue_time": issue, "valid_time": valid, "lead_h": (valid - issue).total_seconds() / 3600,
            "member": member, "quantile": np.nan, "qualifier": qualifier, "value": values, "fetched_at": pd.Timestamp(fetched),
        }
    )


def test_to_forecasts_maps_datasets_and_splits_nwm_members():
    raw = pd.concat(
        [
            archive_rows("marfc_rvf", "flow_cfs", [1000.0, 1100.0], qualifier="usgs_rating:01427510:17.0"),
            archive_rows("marfc_rvf", "stage_ft", [3.1, 3.2], qualifier="RVFUDE"),
            # The same bulletin also carried by the older PIL: a duplicate issuance.
            archive_rows("marfc_rvf", "stage_ft", [3.1, 3.2], qualifier="RVFNY2", fetched="2026-09-20T15:20Z"),
            *[archive_rows("nwm_medium_range", "flow_cfs", [900.0 + m, 950.0 + m], member=m, location="2617456") for m in range(1, 7)],
            archive_rows("nwm_medium_range", "flow_cfs", [903.5, 953.5], qualifier="mean", location="2617456"),
            archive_rows("marfc_nwps", "pool_elev_ft", [1150.0], usgs=None, location="CNNN6"),
        ],
        ignore_index=True,
    )
    f = to_forecasts(raw, "01427510")

    counts = f.groupby(["model", "variable"]).size().to_dict()
    assert counts == {
        ("marfc_rvf", "discharge"): 2,
        ("marfc_rvf", "stage"): 2,
        ("nwm_medium_range_ensemble", "discharge"): 12,
        ("nwm_medium_range_mem1", "discharge"): 2,
    }
    assert (f["site_id"] == "USGS-01427510").all()
    mem1 = f[f["model"] == "nwm_medium_range_mem1"]
    assert mem1["member"].isna().all() and mem1["value"].tolist() == [901.0, 951.0]
    assert f.loc[f["model"] == "nwm_medium_range_ensemble", "member"].nunique() == 6
    assert f.loc[f["model"] == "marfc_rvf", "lead_h"].min() == pytest.approx(4.25)


def test_read_archive_filters_site_and_reads_state(tmp_path):
    lake = Lake(tmp_path)
    rows = pd.concat([archive_rows("hefs", "flow_cfs", [1.0, 2.0], member=1991), archive_rows("hefs", "flow_cfs", [5.0], member=1991, usgs="01428500", location="BRYN6")])
    lake.write_parquet("normalized/hefs/month=2026-09/20260920T142000Z.parquet", rows)
    lake.write("_state/state.json", json.dumps({"seen": {"hefs": {"a": "x", "b": "y"}}, "cursors": {"hefs": "2026-09-20T12:00:00+00:00"}}).encode())

    f = read_archive(lake, ["hefs", "marfc_nwps"], "USGS-01427510")

    assert f["model"].unique().tolist() == ["hefs"] and len(f) == 2
    assert archive_summary(lake) == {"hefs": {"cursor": "2026-09-20T12:00:00+00:00", "recent_issuances": 2}}


def test_air2stream_round_trips_through_dict():
    model = Air2Stream(params=np.arange(8.0) / 10, q_mean=2500.0, ta_clim=np.linspace(-5, 25, 366), rmse_train=1.5)
    again = Air2Stream.from_dict(json.loads(json.dumps(model.to_dict())))
    assert np.allclose(again.params, model.params) and again.q_mean == model.q_mean and np.allclose(again.ta_clim, model.ta_clim)


def test_cache_snapshot_round_trip(tmp_path):
    lake, cache = Lake(tmp_path / "lake"), tmp_path / "cache"
    (cache / "nwm" / "operational" / "short_range").mkdir(parents=True)
    (cache / "nwm" / "operational" / "short_range" / "2026092000.json").write_text("{}")
    (cache / "forcing").mkdir()
    (cache / "forcing" / "skip.parquet").write_text("x")
    save_cache(lake, cache)

    restored = tmp_path / "restored"
    assert restore_cache(lake, restored) > 0
    assert (restored / "nwm" / "operational" / "short_range" / "2026092000.json").exists()
    assert not (restored / "forcing").exists()


def _hourly_obs(days: int) -> pd.Series:
    idx = pd.date_range("2000-10-01", periods=24 * days, freq="h", tz="UTC")
    return pd.Series(1500 + 500 * np.sin(np.arange(len(idx)) / 200.0), index=idx)


def test_forward_opponents_need_enough_verified_days_before_scores_show():
    site = get_site("01427510")
    obs = _hourly_obs(365 * 26 + 60)
    issues = pd.date_range("2026-08-01T12:00Z", periods=10, freq="D")
    rows = [
        {"site_id": site.id, "variable": "discharge", "model": "hefs", "issue_time": t, "valid_time": t + pd.Timedelta(hours=h), "value": 1600.0, "member": m}
        for t in issues for h in (6, 24) for m in range(3)
    ]
    rows = normalize_forecasts(pd.DataFrame(rows))
    long = score_forecasts(rows, obs, site)
    short = score_forecasts(rows[rows["issue_time"] < issues[2]], obs, site)
    assert long["info"].iloc[0]["verified_days"] == 10 >= MIN_VERIFIED_DAYS

    payload = build_payload(
        site, {}, {"forward_hefs": long, "forward_marfc": short},
        {"generated": "2026-09-26 15:30 UTC", "fingerprint": "abc", "n_boot": 50}, {"obs_ingest": {}, "obs_last": {}, "archive": {}},
    )
    by_id = {s["id"]: s for s in payload["sections"]}
    assert by_id["forward_hefs"]["scores"] and not by_id["forward_marfc"]["scores"]
    assert [r["model"] for r in payload["glance"]] == ["hefs"]

    page = render_html(payload)
    assert "Forward archive: HEFS ensemble" in page and "Accumulating: 20 issues" not in page
    assert "Accumulating: 2 issues archived, 2 issue days verified" in page
