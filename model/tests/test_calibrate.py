"""Post-hoc calibration: the CRPS estimator, the flow tail stretch, its cell fallbacks and its application to long-format
forecasts per held-out year, and the warm-up temperature offset as fitted and applied by temp-score."""

import numpy as np
import pandas as pd
import pytest
from flowcast_eval.metrics import crps_ensemble

from flowcast_model import tempscore
from flowcast_model.calibrate import (
    ALL_YEARS,
    LEVELS,
    WARMUP_CLIP,
    FlowCalibration,
    FlowTailCalibration,
    apply_long,
    climatology_percentile,
    fair_crps_sorted,
    fit_stretch,
    stretch,
    warmup_coefficients,
    warmup_normal_equations,
)


def test_fair_crps_sorted_matches_harness():
    rng = np.random.default_rng(0)
    x = np.sort(rng.gamma(2.0, 3.0, (50, 20)), axis=1)
    y = rng.gamma(2.0, 3.0, 50)
    np.testing.assert_allclose(fair_crps_sorted(x, y), crps_ensemble(x, y, fair=True), rtol=1e-10)


def test_stretch_identity_and_upper_tail_only():
    rng = np.random.default_rng(1)
    x = np.sort(rng.lognormal(3.0, 0.5, (10, 41)), axis=1)
    delta = np.full(10, 0.1)
    np.testing.assert_allclose(stretch(x, delta, 0.0, 1.0, 1.0), x, rtol=1e-9)
    wide = stretch(x, delta, 0.0, 1.0, 1.5)
    np.testing.assert_allclose(wide[:, :21], x[:, :21], rtol=1e-9)
    assert (wide[:, 21:] > x[:, 21:]).all()
    assert (np.diff(wide, axis=1) >= 0).all()
    shifted = stretch(x, delta, np.log(2.0), 1.0, 1.0)
    np.testing.assert_allclose(shifted[:, 20], 2.0 * (x[:, 20] + 0.1) - 0.1, rtol=1e-9)
    assert (stretch(x, delta, -50.0, 1.0, 1.0) == 0.0).all()


def thin_upper_tail_ensembles(rng, rows, n=40, factor=0.5):
    """Truth is lognormal about each row's median; the ensemble's upper half is too narrow by `factor`."""
    med = rng.lognormal(2.0, 1.0, rows)
    sigma = 0.8
    y = med * np.exp(sigma * rng.standard_normal(rows))
    z = np.sort(sigma * rng.standard_normal((rows, n)), axis=1)
    z = np.where(z > 0, factor * z, z)
    return np.sort(med[:, None] * np.exp(z), axis=1), y


def test_fit_stretch_recovers_a_thin_upper_tail():
    rng = np.random.default_rng(2)
    x, y = thin_upper_tail_ensembles(rng, 20_000)
    shift, s_lo, s_hi = fit_stretch(x, y, np.full(len(y), 1e-6), np.ones(len(y)))
    assert abs(shift) < 0.05
    assert 0.9 < s_lo < 1.15  # the lower half is nearly flat in CRPS at finite ensemble size
    assert 1.7 < s_hi < 2.3


def test_fit_stretch_without_shift_keeps_the_median_and_fits_the_spread():
    rng = np.random.default_rng(2)
    x, y = thin_upper_tail_ensembles(rng, 20_000, n=41)
    y = y * 0.8
    free = fit_stretch(x, y, np.full(len(y), 1e-6), np.ones(len(y)))
    shift, s_lo, s_hi = fit_stretch(x, y, np.full(len(y), 1e-6), np.ones(len(y)), free_shift=False)
    assert free[0] < -0.1
    assert shift == 0.0
    assert s_hi > 1.3
    xc = stretch(x[:50], np.full(50, 1e-6), shift, s_lo, s_hi)
    np.testing.assert_allclose(xc[:, 20], x[:50, 20], rtol=1e-9)


def test_tail_weight_widens_the_upper_tail_further():
    rng = np.random.default_rng(3)
    x, y = thin_upper_tail_ensembles(rng, 20_000, factor=0.8)
    thr = np.quantile(y, 0.9) * np.ones(len(y))
    plain = fit_stretch(x, y, np.full(len(y), 1e-6), np.ones(len(y)))
    tilted = fit_stretch(x, y, np.full(len(y), 1e-6), np.ones(len(y)), tail=(5.0, thr))
    assert tilted[2] >= plain[2] - 0.02


def test_flow_calibration_cells_fall_back_to_bin_then_lead():
    rng = np.random.default_rng(4)
    rows = 9_000
    x, y = thin_upper_tail_ensembles(rng, rows)
    pct = np.where(np.arange(rows) < 8_500, 0.6, 0.995)  # 500 rows in the top bin: only its bin-level cell fits
    rb = np.where(np.arange(rows) % 2 == 0, 0.1, 0.9)
    cal = FlowTailCalibration.fit(24.0, x, y, np.full(rows, 1e-6), np.ones(rows), pct, rb, flash_edges=(0.5,), min_rows=400)
    cells = set(cal.params["cell"])
    top = len(cal.pct_edges)
    assert {-1, FlowTailCalibration.BIN_CELL + 1, FlowTailCalibration.BIN_CELL + top, 2, 3} <= cells
    assert not {top * 2, top * 2 + 1} & cells
    p = cal.params.set_index("cell")[["shift", "s_lo", "s_hi"]]
    got = cal.lookup(24.0, np.array([0.6, 0.995, 0.1]), np.array([0.1, 0.9, 0.9]))
    np.testing.assert_allclose(got[0], p.loc[2])
    np.testing.assert_allclose(got[1], p.loc[FlowTailCalibration.BIN_CELL + top])
    np.testing.assert_allclose(got[2], p.loc[-1])
    np.testing.assert_allclose(cal.lookup(24.0, rows=2), np.tile(p.loc[-1], (2, 1)))
    with pytest.raises(KeyError):
        cal.lookup(48.0, rows=1)


def test_tail_boost_respects_the_budget_and_only_touches_high_forecasts():
    rng = np.random.default_rng(6)
    rows = 4_000
    x, y = thin_upper_tail_ensembles(rng, rows, factor=1.0)
    delta = np.full(rows, 1e-6)
    pct = np.where(np.arange(rows) % 4 == 0, 0.9, 0.3)
    rb = np.zeros(rows)
    site = np.arange(rows) % 20
    pers = y * np.exp(rng.normal(0, 1.0, rows))
    cal = FlowTailCalibration.fit(6.0, x, y, delta, np.ones(rows), pct, rb, flash_edges=(0.5,), min_rows=1_000, conditional=False)
    tight = cal.fit_boost(6.0, x, y, delta, pct, rb, site, pers, budget=1e-9)
    assert cal.boost["kappa"].tolist() == [1.0]
    assert (tight["median_loss"].diff().dropna() >= 0).all()
    cal.fit_boost(6.0, x, y, delta, pct, rb, site, pers, budget=1.0)
    assert cal.boost["kappa"].tolist() == [3.0]
    p = cal.lookup(6.0, np.array([0.3, 0.9]), np.zeros(2))
    np.testing.assert_allclose(p[1, 2], 3.0 * p[0, 2])
    np.testing.assert_allclose(p[1, :2], p[0, :2])


def test_climatology_percentile_handles_flat_quantiles():
    levels = np.linspace(0, 1, 11)
    q = np.array([0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8], float)
    np.testing.assert_allclose(climatology_percentile(np.array([-1.0, 1.5, 9.0]), levels, q), [0.0, 0.35, 1.0])


def flow_calibration(shifts: dict[int, float]) -> FlowCalibration:
    folds = {wy: FlowTailCalibration((0.5,), pd.DataFrame([{"lead_h": 24.0, "cell": -1, "shift": sh, "s_lo": 0.9, "s_hi": 1.6, "n": 1}])) for wy, sh in shifts.items()}
    stats = pd.DataFrame({"delta": [0.5], "rb": [0.3], **{f"q{lv:.3f}": [100.0 * lv] for lv in LEVELS}}, index=pd.Index(["01000000"], name="basin"))
    return FlowCalibration("ens", folds, stats)


def long_flow_forecasts(rng, issues, leads=(24.0, 48.0), n=40) -> pd.DataFrame:
    rows = []
    for t in issues:
        for lead in leads:
            v = rng.lognormal(3.0, 0.6, n)
            rows.append(pd.DataFrame({"model": "ens", "issue_time": t, "lead_h": lead, "valid_time": t + pd.Timedelta(hours=lead), "member": np.arange(n), "value": v}))
    return pd.concat(rows, ignore_index=True)


def test_flow_apply_long_uses_the_held_out_fit_and_matches_the_array_apply(tmp_path):
    rng = np.random.default_rng(7)
    issues = pd.DatetimeIndex(["2021-03-01T12:00", "2022-03-01T12:00", "2024-03-01T12:00"], tz="UTC")
    fc = long_flow_forecasts(rng, issues)
    cal = flow_calibration({2021: 0.1, 2022: -0.2, ALL_YEARS: 0.3})
    out = apply_long(fc, cal, "USGS-01000000")
    got = out[out["model"] == "ens_cal"]
    assert set(got["lead_h"]) == {24.0}  # no fit at 48 h: left out, not passed through uncalibrated
    for t, wy in zip(issues, (2021, 2022, ALL_YEARS)):
        raw = fc[(fc["issue_time"] == t) & (fc["lead_h"] == 24.0)].sort_values("member")
        x = np.sort(raw["value"].to_numpy())[None, :]
        pct = climatology_percentile(np.median(x, axis=1), LEVELS, 100.0 * LEVELS)
        want = cal.folds[wy].apply(x, 24.0, pct, np.array([0.3]), np.array([0.5]))
        have = np.sort(got[got["issue_time"] == t]["value"].to_numpy())
        np.testing.assert_allclose(have, want[0], rtol=1e-9)
    assert apply_long(fc, cal, "USGS-09999999") is fc

    cal.save(tmp_path)
    loaded = FlowCalibration.load(tmp_path)
    assert all(c.boost.empty for c in loaded.folds.values())  # the extra upper-tail boost is off by default
    np.testing.assert_allclose(apply_long(fc, loaded, "01000000")["value"], out["value"], rtol=1e-9)
    pd.DataFrame([{"lead_h": 24.0, "from_pct": 0.0, "kappa": 2.0}]).to_csv(tmp_path / "flow_boost.csv", index=False)
    boosted = apply_long(fc, FlowCalibration.load(tmp_path), "01000000")
    lo = boosted["value"] <= out["value"] + 1e-9
    assert (boosted["value"] >= out["value"] - 1e-9).all() and lo.mean() > 0.4


def test_warmup_coefficients_without_warmup_are_a_constant_offset():
    resid = np.array([0.1, 0.3, 0.5])
    coef = warmup_coefficients(warmup_normal_equations(resid, np.zeros(3)))
    np.testing.assert_allclose(coef, [0.3, 0.0, 0.0], atol=1e-12)


def synthetic_daily_max(rng, n_members=30):
    """Daily-high forecasts at 12Z, lead days 0-2, WY2021-2022, whose median misses the warm-up response: the truth
    is median + 0.4 + b_up max(dT, 0) + 0.03 min(dT, 0), b_up 0.08 for warm-season target dates and 0.02 otherwise."""
    issues = pd.date_range("2020-10-01T12:00", "2022-09-29T12:00", freq="D", tz="UTC")
    days = pd.date_range("2020-10-01", "2022-10-02", freq="D", tz="UTC")
    obs = pd.Series(rng.normal(15, 5, len(days)), index=days)
    frames, warm = [], []
    for k in range(3):
        valid = issues.floor("D") + pd.Timedelta(days=k)
        dt = np.zeros(len(issues)) if k == 0 else rng.normal(0, 5, len(issues))
        b_up = np.where(valid.month.isin(tempscore.WARM_MONTHS), 0.08, 0.02)
        med = obs.reindex(valid).to_numpy() - (0.4 + b_up * np.maximum(dt, 0) + 0.03 * np.minimum(dt, 0)) + rng.normal(0, 0.3, len(issues))
        m = rng.normal(0, 0.2, (len(issues), n_members))
        m = med[:, None] + m - np.median(m, axis=1, keepdims=True)
        frames.append(pd.DataFrame({"model": "t", "variable": "water_temperature_daily_max", "issue_time": np.repeat(issues, n_members), "lead_h": 24.0 * k,
                                    "valid_time": np.repeat(valid, n_members), "member": np.tile(np.arange(n_members), len(issues)), "value": m.ravel()}))
        warm.append(pd.DataFrame({"issue_time": issues, "lead_h": 24.0 * k, "dt": dt}))
    return pd.concat(frames, ignore_index=True), obs, pd.concat(warm, ignore_index=True)


def test_tempscore_warmup_calibration_recovers_slopes_per_season_and_holds_out_the_year():
    rng = np.random.default_rng(5)
    fc, obs, warmup = synthetic_daily_max(rng)
    table = tempscore._members(fc, obs, warmup, regulated=False)
    first = pd.DataFrame(tempscore.site_sums(table))
    coefs = {tuple(r[k] for k in tempscore.CAL_KEYS): warmup_coefficients(r) for _, r in first.iterrows()}
    cal = tempscore.fit_calibration(pd.DataFrame(tempscore.site_sums(table, coefs)))
    assert set(cal["wy"]) == {2021, 2022, ALL_YEARS} and set(cal["group"]) == {0, 1}
    t = cal.set_index(["wy", "group", "lead_h"])
    for wy in (2021, 2022, ALL_YEARS):
        assert abs(t.loc[(wy, 1, 48.0), "b_up"] - 0.08) < 0.015 and abs(t.loc[(wy, 0, 48.0), "b_up"] - 0.02) < 0.015
    assert (t["scale"] > 1.0).all()
    # each year's coefficients come from the other year only
    other = table[table.index.get_level_values("wy") == 2022]
    sub = other[(other.index.get_level_values("group") == 1) & (other.index.get_level_values("lead_h") == 48.0)]
    med = np.median(sub.drop(columns=["_obs", "_dt"]).to_numpy(), axis=1)
    d = np.clip(sub["_dt"], -WARMUP_CLIP, WARMUP_CLIP)
    want = np.linalg.lstsq(np.column_stack([np.ones(len(sub)), np.maximum(d, 0), np.minimum(d, 0)]), sub["_obs"] - med, rcond=None)[0]
    np.testing.assert_allclose(t.loc[(2021, 1, 48.0), ["a", "b_up", "b_down"]].to_numpy(float), want, rtol=1e-6)

    out = tempscore.apply_calibration(fc, cal, warmup)
    calibrated = out[out["model"] == "t_cal"].groupby(["issue_time", "lead_h", "valid_time"])["value"].median().reset_index()
    resid = obs.reindex(pd.DatetimeIndex(calibrated["valid_time"])).to_numpy() - calibrated["value"].to_numpy()
    dt = calibrated.merge(warmup, on=["issue_time", "lead_h"])["dt"].to_numpy()
    assert abs(resid.mean()) < 0.03
    assert abs(np.corrcoef(resid[dt != 0], dt[dt != 0])[0, 1]) < 0.05
    hourly = fc.assign(variable="water_temperature")
    hcal = tempscore.fit_calibration(pd.DataFrame(tempscore.site_sums(tempscore._members(hourly, obs, None, regulated=True), {})))
    assert (hcal[["b_up", "b_down"]].abs() < 1e-9).all().all() and set(hcal["group"]) == {0}
