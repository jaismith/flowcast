"""Post-hoc calibration: the CRPS estimator, the flow tail stretch and its cell fallbacks, the warm-up temperature offset."""

import numpy as np
import pytest
from flowcast_eval.metrics import crps_ensemble

from flowcast_model.calibrate import FlowTailCalibration, TempWarmupCalibration, climatology_percentile, fair_crps_sorted, fit_stretch, stretch


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


def test_climatology_percentile_handles_flat_quantiles():
    levels = np.linspace(0, 1, 11)
    q = np.array([0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8], float)
    np.testing.assert_allclose(climatology_percentile(np.array([-1.0, 1.5, 9.0]), levels, q), [0.0, 0.35, 1.0])


def test_warmup_offset_recovers_slopes_and_groups():
    rng = np.random.default_rng(5)
    rows = 6_000
    lead = rng.integers(0, 3, rows)
    dt = np.where(lead == 0, 0.0, rng.normal(0, 5, rows))
    group = rng.integers(0, 2, rows)
    slope = np.where(group == 1, 0.08, 0.02)
    med = rng.normal(20, 3, rows)
    obs = med + 0.4 + slope * np.maximum(dt, 0) + 0.03 * np.minimum(dt, 0) + rng.normal(0, 0.3, rows)
    members = med[:, None] + rng.normal(0, 0.2, (rows, 30))
    members -= np.median(members, axis=1, keepdims=True) - med[:, None]
    cal = TempWarmupCalibration.fit(members, obs, lead, dt, group=group)
    t = cal.table.set_index(["group", "lead_day"])
    assert abs(t.loc[(1, 2), "b_up"] - 0.08) < 0.01 and abs(t.loc[(0, 2), "b_up"] - 0.02) < 0.01
    assert abs(t.loc[(0, 0), "a"] - (obs - med)[(group == 0) & (lead == 0)].mean()) < 1e-9
    assert (t["scale"] > 1.0).all()
    out = cal.apply(members, lead, dt, group)
    resid = obs - np.median(out, axis=1)
    assert abs(resid.mean()) < 0.02
    assert abs(np.corrcoef(resid[lead > 0], dt[lead > 0])[0, 1]) < 0.05
    flat = TempWarmupCalibration.fit(members, obs, lead, dt, warmup=False)
    assert (flat.table[["b_up", "b_down"]] == 0).all().all()
    with pytest.raises(KeyError):
        cal.apply(members[:1], np.array([5]), dt[:1], group[:1])
