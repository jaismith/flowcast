"""Scores by lead time with moving-block bootstrap confidence intervals (plan §8.2).

Resampling unit: calendar days of issue time, in contiguous blocks (default 7 days, 1,000 draws), so
serial correlation between nearby issues is respected. The same draws are used for every model and
lead, which makes model-vs-reference differences paired.
"""

import numpy as np
import pandas as pd

from .metrics import HIGHER_IS_BETTER, METRICS, metrics_from_sums, pair_sums
from .pairs import align_issue_times
from .protocol import HindcastProtocol

PAIRED_METRICS = ["mae", "rmse", "crps", "nse", "kge"]
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def block_bootstrap_counts(n_days: int, block_days: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """[n_boot, n_days] matrix of how often each day appears in each moving-block resample."""
    block_days = max(1, min(block_days, n_days))
    n_blocks = -(-n_days // block_days)
    starts = rng.integers(0, n_days - block_days + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_days)[None, None, :]).reshape(n_boot, -1)[:, :n_days]
    counts = np.zeros((n_boot, n_days))
    np.add.at(counts, (np.repeat(np.arange(n_boot), n_days), idx.ravel()), 1.0)
    return counts


def _daily_sums(pairs: pd.DataFrame, days: pd.DatetimeIndex) -> np.ndarray:
    sums = pair_sums(pairs["point"].to_numpy(), pairs["obs"].to_numpy(), pairs["crps"].to_numpy(), pairs["lo"].to_numpy(), pairs["hi"].to_numpy())
    day_pos = days.get_indexer(pairs["issue_time"].dt.floor("D"))
    out = np.zeros((len(days), sums.shape[1]))
    np.add.at(out, day_pos, sums)
    return out


def score_pairs(
    pairs: pd.DataFrame,
    protocol: HindcastProtocol,
    reference: str | None = "persistence",
    by: list[str] | None = None,
    align: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (scores, paired).

    scores: [*by, model, lead_h, metric, value, lo, hi]
    paired: [*by, model, reference, lead_h, metric, diff, lo, hi, skill, skill_lo, skill_hi, better]
      `diff` = model - reference; `skill` = 1 - model/reference for error metrics (MAE/RMSE/CRPS skill score);
      `better` is True when the CI shows the model is significantly better, False when significantly worse.
    """
    by = by or []
    if align:
        pairs = align_issue_times(pairs)
    pairs = pairs.dropna(subset=["point", "obs"])
    if pairs.empty:
        return pd.DataFrame(), pd.DataFrame()
    first, last = pairs["issue_time"].min().floor("D"), pairs["issue_time"].max().floor("D")
    days = pd.date_range(first, last, freq="D")
    rng = np.random.default_rng(protocol.seed)
    counts = block_bootstrap_counts(len(days), protocol.block_days, protocol.n_boot, rng)
    alpha = (1.0 - protocol.ci) / 2.0

    score_rows, paired_rows = [], []
    group_cols = [*by, "lead_h"]
    for key, g in pairs.groupby(group_cols, sort=True, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        labels = dict(zip(group_cols, key))
        draws: dict[str, dict[str, np.ndarray]] = {}
        for model, gm in g.groupby("model", sort=True):
            daily = _daily_sums(gm, days)
            point = metrics_from_sums(daily.sum(axis=0))
            boot = metrics_from_sums(counts @ daily)
            draws[model] = boot
            for metric in METRICS:
                finite = metric != "n" and np.isfinite(boot[metric]).any()
                lo, hi = np.nanpercentile(boot[metric], [100 * alpha, 100 * (1 - alpha)]) if finite else (np.nan, np.nan)
                score_rows.append({**labels, "model": model, "metric": metric, "value": float(point[metric]), "lo": lo, "hi": hi})
            draws[model]["_point"] = point
        if reference is None or reference not in draws:
            continue
        ref = draws[reference]
        for model, boot in draws.items():
            if model == reference:
                continue
            for metric in PAIRED_METRICS:
                diff_point = boot["_point"][metric] - ref["_point"][metric]
                diff = boot[metric] - ref[metric]
                lo, hi = np.nanpercentile(diff, [100 * alpha, 100 * (1 - alpha)])
                if metric in HIGHER_IS_BETTER:
                    skill_point, skill = np.nan, np.full_like(diff, np.nan)
                    better = True if lo > 0 else False if hi < 0 else None
                else:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        skill_point = 1.0 - boot["_point"][metric] / ref["_point"][metric]
                        skill = 1.0 - boot[metric] / ref[metric]
                    better = True if hi < 0 else False if lo > 0 else None
                s_lo, s_hi = np.nanpercentile(skill, [100 * alpha, 100 * (1 - alpha)]) if np.isfinite(skill).any() else (np.nan, np.nan)
                paired_rows.append(
                    {
                        **labels,
                        "model": model,
                        "reference": reference,
                        "metric": metric,
                        "diff": float(diff_point),
                        "lo": lo,
                        "hi": hi,
                        "skill": float(skill_point),
                        "skill_lo": s_lo,
                        "skill_hi": s_hi,
                        "better": better,
                    }
                )
    scores = pd.DataFrame(score_rows)
    paired = pd.DataFrame(paired_rows)
    order = [*by, "model", "lead_h", "metric"]
    scores = scores[order + ["value", "lo", "hi"]]
    if not paired.empty:
        paired = paired[[*by, "model", "reference", "lead_h", "metric", "diff", "lo", "hi", "skill", "skill_lo", "skill_hi", "better"]]
    return scores, paired


def add_season(pairs: pd.DataFrame) -> pd.DataFrame:
    return pairs.assign(season=pairs["issue_time"].dt.month.map(SEASONS))


def add_flow_regime(pairs: pd.DataFrame, q25: float, q90: float, action_flow: float | None = None) -> pd.DataFrame:
    """Regimes from climatological obs quantiles (training years): low < Q25 <= mid <= Q90 < high."""
    regime = np.where(pairs["obs"] < q25, "low", np.where(pairs["obs"] > q90, "high", "mid"))
    out = pairs.assign(regime=regime)
    if action_flow is not None:
        out["above_action"] = pairs["obs"] >= action_flow
    return out


def table(scores: pd.DataFrame, metric: str, leads: list[float] | None = None, fmt: str = "{:.3f}", ci: bool = True) -> pd.DataFrame:
    """Model x lead table for one metric, with CIs as `value [lo, hi]`."""
    s = scores[scores["metric"] == metric]
    if leads is not None:
        s = s[s["lead_h"].isin(leads)]

    def cell(row):
        if not ci or np.isnan(row["lo"]):
            return fmt.format(row["value"])
        return f"{fmt.format(row['value'])} [{fmt.format(row['lo'])}, {fmt.format(row['hi'])}]"

    return s.assign(cell=s.apply(cell, axis=1)).pivot(index="model", columns="lead_h", values="cell")
