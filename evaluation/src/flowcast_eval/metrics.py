"""Verification metrics.

Two layers:

* Per-pair scores (`crps_*`, `interval_hits`) turn a forecast (deterministic, ensemble, or quantiles)
  and an observation into numbers that can be summed.
* Aggregate metrics (NSE, KGE, MAE, RMSE, bias, CRPS, coverage) are computed from additive sufficient
  statistics (`SUMS`), so bootstrap resampling only has to re-add per-day sums.
"""

import numpy as np

SUMS = ["n", "so", "soo", "sf", "sff", "sfo", "sae", "sse", "scrps", "ncov", "scov"]
METRICS = ["n", "nse", "kge", "kge_r", "kge_alpha", "kge_beta", "mae", "rmse", "bias", "pbias", "crps", "coverage80"]
# Metrics where larger is better; everything else (except n, bias, coverage) is an error.
HIGHER_IS_BETTER = {"nse", "kge", "kge_r"}


# ---------------------------------------------------------------- per-pair scores


def crps_ensemble(ens: np.ndarray, obs: np.ndarray, fair: bool = True) -> np.ndarray:
    """CRPS of each ensemble row `ens[i, :]` (NaN members ignored) against `obs[i]`.

    `fair=True` uses the unbiased estimator of Ferro et al. (2008), which removes the dependence
    on ensemble size so a 6-member NWM ensemble and a 65-member HEFS ensemble are comparable.
    """
    ens = np.asarray(ens, float)
    obs = np.asarray(obs, float)
    x = np.sort(ens, axis=1)  # NaNs sort last
    valid = ~np.isnan(x)
    m = valid.sum(axis=1)
    xs = np.where(valid, x, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        abs_err = np.where(valid, np.abs(x - obs[:, None]), 0.0).sum(axis=1) / m
        rank = np.arange(1, x.shape[1] + 1)[None, :]
        # sum_{i,j} |x_i - x_j| = 2 * sum_i (2i - m - 1) x_(i) over sorted valid members
        spread = 2.0 * np.where(valid, (2 * rank - m[:, None] - 1) * xs, 0.0).sum(axis=1)
        denom = 2.0 * m * (m - 1) if fair else 2.0 * m * m
        crps = abs_err - np.where(m > 1, spread / denom, 0.0)
    crps[(m == 0) | np.isnan(obs)] = np.nan
    return crps


def quantile_weights(levels: np.ndarray) -> np.ndarray:
    """Integration weights for quantile levels over (0, 1) (midpoint rule)."""
    levels = np.asarray(levels, float)
    edges = np.concatenate([[0.0], (levels[1:] + levels[:-1]) / 2.0, [1.0]])
    return np.diff(edges)


def crps_quantiles(q: np.ndarray, levels: np.ndarray, obs: np.ndarray) -> np.ndarray:
    """CRPS approximated as 2 * integral of the pinball loss over quantile levels (Gneiting & Raftery 2007)."""
    q = np.sort(np.asarray(q, float), axis=1)
    levels = np.asarray(levels, float)
    obs = np.asarray(obs, float)[:, None]
    diff = obs - q
    pinball = np.where(diff >= 0, levels * diff, (levels - 1.0) * diff)
    return 2.0 * np.sum(pinball * quantile_weights(levels), axis=1)


def ensemble_summary(ens: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Median and 10th/90th percentiles of each ensemble row."""
    with np.errstate(all="ignore"):
        lo, med, hi = np.nanpercentile(ens, [10, 50, 90], axis=1)
    return med, lo, hi


def quantile_summary(q: np.ndarray, levels: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    q = np.sort(np.asarray(q, float), axis=1)
    levels = np.asarray(levels, float)

    def at(level: float) -> np.ndarray:
        if level < levels[0] or level > levels[-1]:
            return np.full(q.shape[0], np.nan)
        return np.array([np.interp(level, levels, row) for row in q])

    return at(0.5), at(0.1), at(0.9)


# ------------------------------------------------------------- aggregate metrics


def pair_sums(point: np.ndarray, obs: np.ndarray, crps: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Per-pair contributions to `SUMS` (shape [n, len(SUMS)]). Rows with NaN point/obs contribute zero."""
    f, o = np.asarray(point, float), np.asarray(obs, float)
    ok = ~(np.isnan(f) | np.isnan(o))
    f0, o0 = np.where(ok, f, 0.0), np.where(ok, o, 0.0)
    c = np.where(ok, np.nan_to_num(np.asarray(crps, float), nan=0.0), 0.0)
    has_interval = ok & ~(np.isnan(lo) | np.isnan(hi))
    inside = has_interval & (o0 >= np.nan_to_num(lo)) & (o0 <= np.nan_to_num(hi))
    err = f0 - o0
    return np.stack(
        [ok, o0, o0 * o0, f0, f0 * f0, f0 * o0, np.abs(err), err * err, c, has_interval, inside],
        axis=-1,
    ).astype(float)


def metrics_from_sums(sums: np.ndarray) -> dict[str, np.ndarray]:
    """Aggregate metrics from summed statistics; works on any leading shape (e.g. bootstrap draws)."""
    s = {name: sums[..., i] for i, name in enumerate(SUMS)}
    n = s["n"]
    with np.errstate(invalid="ignore", divide="ignore"):
        mo, mf = s["so"] / n, s["sf"] / n
        vo = np.maximum(s["soo"] / n - mo * mo, 0.0)
        vf = np.maximum(s["sff"] / n - mf * mf, 0.0)
        cov = s["sfo"] / n - mf * mo
        r = cov / np.sqrt(vo * vf)
        alpha = np.sqrt(vf / vo)
        beta = mf / mo
        return {
            "n": n,
            "nse": 1.0 - s["sse"] / (n * vo),
            "kge": 1.0 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2),
            "kge_r": r,
            "kge_alpha": alpha,
            "kge_beta": beta,
            "mae": s["sae"] / n,
            "rmse": np.sqrt(s["sse"] / n),
            "bias": mf - mo,
            "pbias": 100.0 * (mf - mo) / mo,
            "crps": s["scrps"] / n,
            "coverage80": s["scov"] / s["ncov"],
        }


def score(point, obs, crps=None, lo=None, hi=None) -> dict[str, float]:
    """Convenience: all aggregate metrics for aligned arrays."""
    point = np.asarray(point, float)
    obs = np.asarray(obs, float)
    crps = np.abs(point - obs) if crps is None else np.asarray(crps, float)
    nan = np.full_like(point, np.nan)
    sums = pair_sums(point, obs, crps, nan if lo is None else np.asarray(lo, float), nan if hi is None else np.asarray(hi, float))
    return {k: float(v) for k, v in metrics_from_sums(sums.sum(axis=0)).items()}


def nse(sim, obs) -> float:
    return score(sim, obs)["nse"]


def kge(sim, obs) -> float:
    """Kling-Gupta efficiency (Gupta et al. 2009)."""
    return score(sim, obs)["kge"]
