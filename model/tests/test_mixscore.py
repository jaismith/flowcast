import numpy as np
import pandas as pd
import pytest
from scipy import integrate

from flowcast_eval.metrics import crps_ensemble
from flowcast_model.cli import main
from flowcast_model.ensemble import sample_cmal
from flowcast_model.mixscore import LEVELS, mixture_pairs, mixture_scores, pooled_mixture, scored_cells
from flowcast_model.units import to_cfs

ISSUES = pd.DatetimeIndex(["2021-01-01T12:00", "2021-01-02T00:00", "2021-01-02T03:00", "2021-01-03T18:00"], tz="UTC")
LEADS = [1, 2, 6]


def _cdf(x, w, mu, b, tau):
    z = x - mu
    lower = tau * np.exp((1 - tau) / b * np.minimum(z, 0))
    upper = 1 - (1 - tau) * np.exp(-tau / b * np.maximum(z, 0))
    return float(np.sum(w / w.sum() * np.where(z < 0, lower, upper)))


def _crps_by_quadrature(y, w, mu, b, tau):
    """CRPS of the clipped mixture as the integral of (F - 1{x >= y})^2, split at its kinks."""
    edges = sorted({0.0, max(y, 0.0), *[m for m in mu if m > 0]})
    edges.append(edges[-1] + 80 * b.max() / min(tau.min(), 1 - tau.max()))
    total = sum(integrate.quad(lambda x: (_cdf(x, w, mu, b, tau) - (x >= y)) ** 2, lo, hi, epsabs=1e-14, epsrel=1e-12, limit=500)[0] for lo, hi in zip(edges[:-1], edges[1:]))
    return total + max(-y, 0.0)


def _random_mixture(rng, k):
    return rng.random(k), rng.normal(0.5, 1.0, k), np.exp(rng.normal(-1.5, 1.0, k)), rng.uniform(0.02, 0.98, k)


def test_crps_matches_quadrature_of_the_clipped_cdf():
    rng = np.random.default_rng(1)
    for _ in range(120):
        w, mu, b, tau = _random_mixture(rng, int(rng.integers(1, 10)))
        y = float(rng.choice([rng.normal(0.5, 1.5), 0.0, -0.3, mu[0]]))
        crps, _ = mixture_scores(w[None], mu[None], b[None], tau[None], np.array([y]), LEVELS)
        assert crps[0] == pytest.approx(_crps_by_quadrature(y, w, mu, b, tau), rel=1e-10, abs=1e-14)


def test_crps_of_a_laplace_far_from_zero_is_the_textbook_formula():
    mu, b, y = 50.0, np.array([0.7]), np.array([47.0, 50.0, 52.5])
    s = 2 * b[0]  # tau = 0.5: a Laplace with scale 2b
    crps, q = mixture_scores(np.ones((3, 1)), np.full((3, 1), mu), np.tile(b, (3, 1)), np.full((3, 1), 0.5), y, LEVELS)
    d = np.abs(y - mu)
    np.testing.assert_allclose(crps, d + s * np.exp(-d / s) - 0.75 * s, rtol=1e-12)
    np.testing.assert_allclose(q[0], [mu + s * np.log(0.2), mu, mu - s * np.log(0.2)], rtol=1e-12)


def test_quantiles_are_the_clipped_mixtures():
    rng = np.random.default_rng(2)
    for _ in range(200):
        w, mu, b, tau = _random_mixture(rng, int(rng.integers(1, 10)))
        _, q = mixture_scores(w[None], mu[None], b[None], tau[None], np.array([np.nan]), LEVELS)
        for level, x in zip(LEVELS, q[0]):
            if x == 0.0:
                assert _cdf(0.0, w, mu, b, tau) >= level
            else:
                assert _cdf(x, w, mu, b, tau) == pytest.approx(level, abs=1e-10)


def test_sampled_fair_crps_converges_to_the_exact_crps():
    rng = np.random.default_rng(3)
    w, mu, b, tau = _random_mixture(rng, 6)
    y = 0.4
    exact, _ = mixture_scores(w[None], mu[None], b[None], tau[None], np.array([y]), LEVELS)
    draws = np.clip(sample_cmal(np.tile(w, (400, 1)), np.tile(mu, (400, 1)), np.tile(b, (400, 1)), np.tile(tau, (400, 1)), 500, rng), 0, None)
    sampled = crps_ensemble(draws, np.full(400, y))
    assert abs(sampled.mean() - exact[0]) < 4 * sampled.std() / np.sqrt(len(sampled))


def _frame(mu: float, members: list[int], b: float = 0.3, tau: float = 0.4, pi: tuple = (2.0, 0.0, 0.0)) -> pd.DataFrame:
    rows = [(i, float(lead), m) for i in ISSUES for lead in LEADS for m in members]
    frame = pd.DataFrame(rows, columns=["issue_time", "lead_h", "member"])
    frame["unit"] = "mm/h"
    for c in range(3):
        frame[f"pi{c}"], frame[f"mu{c}"], frame[f"b{c}"], frame[f"tau{c}"] = pi[c], mu + c, b, tau
    return frame


def test_pairs_pool_seeds_equally_in_ft3_s_on_cycle_issues_only():
    area = 450.0
    factor = float(to_cfs(np.ones(1), "mm/h", area)[0])
    obs = pd.Series(1.7 * factor, index=pd.date_range("2020-12-31", "2021-01-05", freq="h", tz="UTC"))
    window = (pd.Timestamp("2020-10-01", tz="UTC"), pd.Timestamp("2021-01-03T19:00", tz="UTC"))
    frames = [_frame(1.0, [0, 1]), _frame(2.5, [0, 1])]
    frames = [scored_cells(f, (1.0, 6.0), (0, 6, 12, 18), window) for f in frames]
    pairs = mixture_pairs(frames, obs, "USGS-01000002", "pool", "operational", area, window[1])

    assert sorted(pairs["issue_time"].unique()) == [ISSUES[0], ISSUES[1], ISSUES[3]]
    assert sorted(pairs["lead_h"].unique()) == [1.0, 6.0]
    late = pairs["valid_time"] > window[1]
    assert late.any() and pairs.loc[late, "obs"].isna().all() and pairs.loc[late, "crps"].isna().all()
    w, mu, b, tau = (np.array([[0.5, 0.5]]), np.array([[1.0, 2.5]]), np.full((1, 2), 0.3), np.full((1, 2), 0.4))
    crps, q = mixture_scores(w, mu, b, tau, np.array([1.7]), LEVELS)
    on_time = pairs[~late]
    np.testing.assert_allclose(on_time["crps"], crps[0] * factor, rtol=1e-12)
    np.testing.assert_allclose(on_time[["lo", "point", "hi"]].to_numpy(), np.tile(q[0] * factor, (len(on_time), 1)), rtol=1e-12)


def _write_seed(root, site: str, mu: float, b: float) -> str:
    folder = root / f"site_id={site}"
    folder.mkdir(parents=True)
    _frame(mu, [0, 1], b=b, tau=0.3, pi=(0.7, 0.3, 0.0)).to_parquet(folder / "lstm.parquet", index=False)
    _frame(mu, [-1], b=b, tau=0.6, pi=(0.6, 0.4, 0.0)).to_parquet(folder / "lstm_perfect.parquet", index=False)
    return str(root)


def test_score_with_mixtures_matches_the_sampled_ensemble(tmp_path, cube_path):
    site = "USGS-01000002"
    runs = [_write_seed(tmp_path / "s42", site, 0.02, 0.02), _write_seed(tmp_path / "s43", site, 0.06, 0.03)]
    _write_seed(tmp_path / "s42", "USGS-01000001", 0.02, 0.02)  # only in one seed: skipped
    sampled = tmp_path / "sampled"
    main(["mixture-ensemble", "--mixtures", *runs, "--out", str(sampled), "--model", "pool_sampled", "--cube", str(cube_path), "--samples-per-member", "2500"])
    out = tmp_path / "scores"
    main(["score", "--forecasts", str(sampled), "--mixtures", f"pool={','.join(runs)}", "--cube", str(cube_path), "--out", str(out), "--target", "qobs", "--nwm-attribute", "", "--n-boot", "20"])

    scores = pd.read_csv(out / "scores.csv")
    assert set(scores["site_id"]) == {site}
    assert {"pool", "pool_perfect", "pool_sampled", "pool_sampled_perfect", "persistence"} <= set(scores["model"])
    value = scores.set_index(["model", "lead_h", "metric"])["value"]
    for exact, approx in (("pool", "pool_sampled"), ("pool_perfect", "pool_sampled_perfect")):
        for lead in LEADS:
            assert value[exact, lead, "n"] == value[approx, lead, "n"] > 0
            assert value[exact, lead, "crps"] == pytest.approx(value[approx, lead, "crps"], rel=0.02)
            assert value[exact, lead, "mae"] == pytest.approx(value[approx, lead, "mae"], rel=0.02)
            assert 0.0 <= value[exact, lead, "coverage80"] <= 1.0
    paired = pd.read_csv(out / "paired.csv")
    assert ((paired["model"] == "pool") & (paired["reference"] == "persistence")).any()


def test_pooling_keeps_a_saturated_asymmetry_scorable():
    frames = [_frame(1.0, [0, 1], tau=1.0), _frame(2.5, [0, 1], tau=0.0)]
    _, _, (w, mu, b, tau), _ = pooled_mixture(frames)
    assert 0.0 < tau.min() and tau.max() < 1.0
    n = w.shape[0] * w.shape[1]
    crps, q = mixture_scores(*(x.reshape(n, -1) for x in (w, mu, b, tau)), np.full(n, 1.7), LEVELS)
    assert np.isfinite(crps).all() and np.isfinite(q).all()
