from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from flowcast_eval.pairs import PAIR_COLUMNS, align_issue_times
from flowcast_eval.protocol import FROZEN_TEST, lead_bin
from flowcast_eval.scoring import add_flow_regime, add_season, block_bootstrap_counts, score_pairs

PROTO = replace(FROZEN_TEST, n_boot=300)


def make_pairs(model, point, obs, issues, lead=24.0):
    return pd.DataFrame(
        {
            "model": model,
            "site_id": "USGS-1",
            "variable": "discharge",
            "run_type": "operational",
            "issue_time": issues,
            "valid_time": issues + pd.Timedelta(hours=lead),
            "lead_h": lead,
            "point": point,
            "crps": np.abs(point - obs),
            "lo": np.nan,
            "hi": np.nan,
            "obs": obs,
        }
    )[PAIR_COLUMNS]


def test_block_bootstrap_counts():
    counts = block_bootstrap_counts(30, 7, 50, np.random.default_rng(0))
    assert counts.shape == (50, 30)
    np.testing.assert_allclose(counts.sum(axis=1), 30)


def test_better_model_is_significant_and_identical_is_not():
    rng = np.random.default_rng(3)
    issues = pd.date_range("2023-01-01", periods=400, freq="6h", tz="UTC")
    obs = 1000 + 300 * np.sin(np.arange(400) / 20) + rng.normal(0, 20, 400)
    pairs = pd.concat(
        [
            make_pairs("persistence", obs + rng.normal(0, 200, 400), obs, issues),
            make_pairs("good", obs + rng.normal(0, 50, 400), obs, issues),
            make_pairs("twin", obs + rng.normal(0, 200, 400), obs, issues),
        ]
    )
    pairs.loc[pairs["model"] == "twin", "point"] = pairs.loc[pairs["model"] == "persistence", "point"].to_numpy()
    pairs["crps"] = (pairs["point"] - pairs["obs"]).abs()

    scores, paired = score_pairs(pairs, PROTO)

    mae = scores[(scores["metric"] == "mae")].set_index("model")
    assert mae.loc["good", "value"] < mae.loc["persistence", "value"]
    assert mae.loc["good", "lo"] <= mae.loc["good", "value"] <= mae.loc["good", "hi"]
    good = paired[(paired["model"] == "good") & (paired["metric"] == "crps")].iloc[0]
    assert good["better"] is True
    assert 0 < good["skill_lo"] <= good["skill"] <= good["skill_hi"] < 1
    twin = paired[(paired["model"] == "twin") & (paired["metric"] == "mae")].iloc[0]
    assert twin["diff"] == pytest.approx(0) and twin["better"] is None


def test_alignment_keeps_common_issue_times():
    issues = pd.date_range("2023-01-01", periods=4, freq="6h", tz="UTC")
    a = make_pairs("a", np.ones(4), np.ones(4), issues)
    b = make_pairs("b", np.ones(3), np.ones(3), issues[1:])
    aligned = align_issue_times(pd.concat([a, b]))
    assert set(aligned["issue_time"]) == set(issues[1:])
    assert len(aligned) == 6


def test_breakdowns():
    issues = pd.date_range("2023-01-01", periods=4, freq="90D", tz="UTC")
    p = make_pairs("a", np.ones(4), np.array([1.0, 50.0, 200.0, 500.0]), issues)
    p = add_flow_regime(add_season(p), q25=10, q90=300, action_flow=400)
    assert p["season"].tolist() == ["DJF", "MAM", "JJA", "SON"]
    assert p["regime"].tolist() == ["low", "mid", "mid", "high"]
    assert p["above_action"].tolist() == [False, False, False, True]


def test_lead_bin_and_issue_times():
    bins = lead_bin(pd.Series([0.0, 0.5, 1.0, 4.2, 168.0, 170.0]), (1, 3, 6, 168))
    assert bins.tolist()[1:5] == [1.0, 1.0, 6.0, 168.0]
    assert np.isnan(bins.iloc[0]) and np.isnan(bins.iloc[5])
    assert lead_bin(pd.Series([0.0, 24.0]), (0, 24, 48)).tolist() == [0.0, 24.0]
    issues = FROZEN_TEST.issue_times(until=pd.Timestamp("2022-10-02T07:00", tz="UTC"))
    assert [t.hour for t in issues] == [0, 6, 12, 18, 0, 6]
