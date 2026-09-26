import numpy as np
import pytest
from scipy.stats import norm

from flowcast_eval.metrics import crps_ensemble, crps_quantiles, kge, metrics_from_sums, nse, pair_sums, score


def brute_crps(ens, y, fair):
    m = len(ens)
    a = np.mean(np.abs(ens - y))
    b = np.abs(ens[:, None] - ens[None, :]).sum()
    return a - b / (2 * m * (m - 1)) if fair else a - b / (2 * m * m)


def test_nse_and_kge_reference_points():
    rng = np.random.default_rng(0)
    obs = rng.gamma(2.0, 100.0, 500)
    assert nse(obs, obs) == pytest.approx(1.0)
    assert kge(obs, obs) == pytest.approx(1.0)
    assert nse(np.full_like(obs, obs.mean()), obs) == pytest.approx(0.0, abs=1e-12)
    # Doubling the simulation: r=1, alpha=2, beta=2 -> KGE = 1 - sqrt(2)
    assert kge(2 * obs, obs) == pytest.approx(1 - np.sqrt(2))


def test_sums_match_direct_formulas():
    rng = np.random.default_rng(1)
    obs = rng.normal(50, 10, 200)
    sim = obs + rng.normal(2, 5, 200)
    s = score(sim, obs)
    assert s["nse"] == pytest.approx(1 - np.sum((sim - obs) ** 2) / np.sum((obs - obs.mean()) ** 2))
    r = np.corrcoef(sim, obs)[0, 1]
    alpha, beta = sim.std() / obs.std(), sim.mean() / obs.mean()
    assert s["kge"] == pytest.approx(1 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2))
    assert s["mae"] == pytest.approx(np.mean(np.abs(sim - obs)))
    assert s["rmse"] == pytest.approx(np.sqrt(np.mean((sim - obs) ** 2)))
    assert s["crps"] == pytest.approx(s["mae"])


def test_nan_pairs_are_ignored():
    obs = np.array([1.0, 2.0, np.nan, 4.0])
    sim = np.array([1.0, np.nan, 3.0, 5.0])
    assert score(sim, obs)["n"] == 2
    assert score(sim, obs)["mae"] == pytest.approx(0.5)


@pytest.mark.parametrize("fair", [True, False])
def test_crps_ensemble_matches_brute_force(fair):
    rng = np.random.default_rng(2)
    ens = rng.normal(size=(20, 11))
    obs = rng.normal(size=20)
    got = crps_ensemble(ens, obs, fair=fair)
    want = [brute_crps(e, y, fair) for e, y in zip(ens, obs)]
    np.testing.assert_allclose(got, want, rtol=1e-10)


def test_crps_ensemble_handles_missing_members_and_single_member():
    ens = np.array([[1.0, 2.0, np.nan], [3.0, np.nan, np.nan]])
    obs = np.array([1.5, 1.0])
    got = crps_ensemble(ens, obs)
    assert got[0] == pytest.approx(brute_crps(np.array([1.0, 2.0]), 1.5, True))
    assert got[1] == pytest.approx(2.0)


def test_quantile_crps_approximates_gaussian_closed_form():
    levels = (np.arange(99) + 0.5) / 99
    mu, sigma, y = 10.0, 2.0, 11.3
    q = norm.ppf(levels, mu, sigma)[None, :]
    z = (y - mu) / sigma
    exact = sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / np.sqrt(np.pi))
    assert crps_quantiles(q, levels, np.array([y]))[0] == pytest.approx(exact, rel=0.02)


def test_coverage():
    obs = np.array([0.0, 5.0, 10.0])
    lo, hi = np.array([-1.0, 6.0, 9.0]), np.array([1.0, 7.0, 11.0])
    sums = pair_sums(obs, obs, np.zeros(3), lo, hi).sum(axis=0)
    assert metrics_from_sums(sums)["coverage80"] == pytest.approx(2 / 3)
