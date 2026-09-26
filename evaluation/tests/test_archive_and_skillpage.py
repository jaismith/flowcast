import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from flowcast_pipeline.lake import Lake
from flowcast_pipeline.sites import get_site
from flowcast_eval import archive as archive_module
from flowcast_eval.archive import archive_summary, read_archive, to_forecasts
from flowcast_eval.baselines import Air2Stream
from flowcast_eval.schema import normalize_forecasts
from flowcast_eval.scoreboard import score_forecasts
from flowcast_eval.skillpage import MIN_VERIFIED_DAYS, build_payload, publish, render_html, restore_cache, save_cache

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


def test_zstd_files_fall_back_to_fastparquet(tmp_path, monkeypatch):
    lake = Lake(tmp_path)
    rows = pd.concat([archive_rows("marfc_rvf", "flow_cfs", [1000.0, 1100.0]), archive_rows("marfc_rvf", "flow_cfs", [7.0], usgs="01428500")])
    rows.to_parquet(tmp_path / "zstd.parquet", compression="zstd", index=False)
    lake.write("normalized/marfc_rvf/month=2026-09/seed.parquet", (tmp_path / "zstd.parquet").read_bytes())
    expected = read_archive(lake, ["marfc_rvf"], "01427510")

    def no_zstd(*args, **kwargs):
        raise pa.ArrowNotImplementedError("Support for codec 'zstd' not built")

    monkeypatch.setattr(archive_module.pq, "read_table", no_zstd)
    fallback = read_archive(lake, ["marfc_rvf"], "01427510")
    pd.testing.assert_frame_equal(fallback, expected, check_dtype=False)
    assert fallback["value"].tolist() == [1000.0, 1100.0]


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


def test_publish_writes_page_scoreboard_and_registry_mirror(tmp_path):
    lake, web = Lake(tmp_path / "lake"), tmp_path / "web"
    site = get_site("01427510")
    publish(lake, str(web), site, {"generated": "x"}, "# md", "<html></html>", pd.Timestamp("2026-09-26T15:30Z"))

    assert sorted(p.relative_to(web).as_posix() for p in web.rglob("*") if p.is_file()) == [
        "index.html", "v1/sites.json", "v1/sites/USGS-01427510/scoreboard.md", "v1/sites/USGS-01427510/skill.json",
    ]
    registry = json.loads((web / "v1" / "sites.json").read_text())
    assert registry["sites"][0]["id"] == "USGS-01427510" and "USGS-01436000" in registry["ingest_gauges"]
    assert lake.read("sites/sites.yaml").startswith(b"# flowcast v2 site registry")
    assert lake.list("metrics/") == ["metrics/USGS-01427510/2026-09-26/scoreboard.md", "metrics/USGS-01427510/2026-09-26/skill.json"]


def test_best_opponent_view_combines_marfc_and_nwm_references():
    def paired(model, lead, skill, better):
        return {"model": model, "reference": "x", "lead_h": lead, "metric": "crps", "diff": 0.0, "lo": 0.0, "hi": 0.0,
                "skill": skill, "skill_lo": skill - 0.1, "skill_hi": skill + 0.1, "better": better}

    marfc = pd.DataFrame([paired("persistence", 24.0, -0.9, False), paired("persistence", 168.0, -2.0, False)])
    nwm = pd.DataFrame([paired("persistence", 24.0, -0.3, False), paired("persistence", 168.0, -0.6, False)])
    strong = {
        "scores": [], "vs_persistence": [], "info": {"issues": 10, "marfc_issues": 5},
        "vs_opponent": [paired("lgbm_qpf", 24.0, 0.14, None) | {"opponent": "marfc_rvf"}, paired("lgbm_qpf", 120.0, 0.42, True) | {"opponent": "nwm_retrospective"}],
    }
    archived = {"marfc_rvf_discharge": {"scores": pd.DataFrame(), "vs_persistence": pd.DataFrame(), "vs_opponent": marfc, "info": pd.DataFrame([{"issues": 5, "verified_days": 5}])}}
    results = {"nwm_operational": {"medium_scores": pd.DataFrame(), "medium_vs_persistence": pd.DataFrame(), "short_scores": pd.DataFrame(),
                                   "short_vs_persistence": pd.DataFrame(), "medium_vs_ensemble": nwm, "info": pd.DataFrame()}}
    payload = build_payload(get_site("01427510"), results, archived, {"generated": "g", "n_boot": 10}, {"obs_ingest": {}, "obs_last": {}, "archive": {}}, strong)

    validation, operational = payload["best_opponent"]
    assert [r["opponent"] for r in validation["rows"]] == ["marfc_rvf", "nwm_retrospective"]
    assert [(r["lead_h"], r["opponent"]) for r in operational["rows"]] == [(24.0, "marfc_rvf"), (168.0, "nwm_medium_range_ensemble")]
    assert payload["sections"][0]["id"] == "strong_baselines"
    page = render_html(payload)
    assert "Skill vs best opponent" in page and "vs MARFC" in page and "vs NWM ensemble" in page and "LightGBM, GEFS QPF" in page
