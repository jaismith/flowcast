"""Quantile mapping of archived forecast forcing onto the observed forcing's distribution (miss-diagnostics fix 1).

Fitted per basin and lead bin on training years only. A forecast value at lead L covers the hours after the previous
lead up to L (the dataset's hour-ending convention), so it is compared with the observed mean over the same hours of
the same issue. Forecast quantiles (members pooled) map to observed quantiles at the same levels; zero stays zero,
and values above the top fitted quantile scale by that quantile's ratio.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .cube import Cube

LEVELS = np.unique(np.concatenate([np.linspace(0.0, 0.9, 91), np.linspace(0.9, 0.99, 46), np.linspace(0.99, 0.9999, 41)]))
LEAD_EDGES = (0.0, 24.0, 48.0, 72.0, 96.0, 120.0, 144.0, np.inf)


def _curve(fc_q: np.ndarray, obs_q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Monotone (forecast, observed) knots through (0, 0); ties in the forecast quantiles keep the first level."""
    wet = fc_q > 0
    xp = np.concatenate([[0.0], fc_q[wet]])
    fp = np.concatenate([[0.0], obs_q[wet]])
    xp, first = np.unique(xp, return_index=True)
    return xp, np.maximum.accumulate(fp[first])


@dataclass
class QuantileMap:
    edges: np.ndarray
    curves: dict[str, list[tuple[np.ndarray, np.ndarray]]]

    def apply(self, basin: str, leads: np.ndarray, values: np.ndarray) -> np.ndarray:
        """Mapped copy of values[issue, lead, ...] (lead on the second axis); basins without a fit are unchanged."""
        if basin not in self.curves:
            return values
        out = np.array(values, dtype=np.float32, copy=True)
        bins = np.clip(np.searchsorted(self.edges, leads, side="left") - 1, 0, len(self.edges) - 2)
        for b, (xp, fp) in enumerate(self.curves[basin]):
            cols = np.flatnonzero(bins == b)
            if not cols.size or len(xp) < 2:
                continue
            x = out[:, cols]
            y = np.interp(x, xp, fp)
            above = x > xp[-1]
            y[above] = x[above] * (fp[-1] / xp[-1])
            y[x <= 0] = 0.0
            y[np.isnan(x)] = np.nan
            out[:, cols] = y
        return out


def load(path: str | Path) -> QuantileMap:
    df = pd.read_parquet(path)
    edges = np.array(sorted(set(df["lead_lo"]) | set(df["lead_hi"])), dtype=float)
    curves: dict[str, list] = {}
    for basin, g in df.groupby("basin", sort=False):
        per_bin = []
        for lo in edges[:-1]:
            gb = g[g["lead_lo"] == lo].sort_values("level")
            per_bin.append(_curve(gb["forecast"].to_numpy(float), gb["observed"].to_numpy(float)) if len(gb) else (np.zeros(1), np.zeros(1)))
        curves[str(basin)] = per_bin
    return QuantileMap(edges, curves)


def fit_basin(cube: Cube, basin: str, forecast: str, observed: str, start: pd.Timestamp, end: pd.Timestamp, edges=LEAD_EDGES) -> pd.DataFrame:
    """Quantiles of `forecast` (issues in [start, end], valid times <= end) and of `observed` over the same hours."""
    product = cube.load_forecast(basin, [forecast], start, end)
    issues, leads, values, _ = next(iter(product.values()))
    obs = cube.load_dynamic(basin, [observed], start, end)[observed]
    spacing = np.diff(leads, prepend=0.0)
    means = {int(s): obs.rolling(int(s), min_periods=int(s)).mean() for s in np.unique(spacing[spacing > 0])}
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        fc, ob = [], []
        for j in np.flatnonzero((leads > lo) & (leads <= hi) & (spacing > 0)):
            mean = means[int(spacing[j])]
            valid = issues + pd.Timedelta(hours=float(leads[j]))
            keep = valid <= end
            o = mean.reindex(valid[keep]).to_numpy()
            f = values[keep, j, :, 0]
            ok = np.isfinite(o) & np.isfinite(f).all(axis=1)
            fc.append(f[ok].ravel())
            ob.append(o[ok])
        if not fc or not sum(len(x) for x in ob):
            continue
        fc, ob = np.concatenate(fc), np.concatenate(ob)
        rows.append(pd.DataFrame({"basin": basin, "lead_lo": lo, "lead_hi": hi, "level": LEVELS, "forecast": np.quantile(fc, LEVELS), "observed": np.quantile(ob, LEVELS), "n": len(ob)}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def fit(cube: Cube, basins: list[str], forecast: str, observed: str, start: str, end: str) -> pd.DataFrame:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    return pd.concat([fit_basin(cube, b, forecast, observed, start, end) for b in basins], ignore_index=True)
