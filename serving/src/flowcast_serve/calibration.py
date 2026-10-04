"""Post-processing of live ensembles with the registry version's calibration (the all-years fit, `wy = 0`).

Flow: the PR #51 conditional stretch (`flowcast_model.calibrate`), fitted at the harness's 22 leads; hours between
two fitted leads use parameters interpolated linearly in lead (beyond 168 h, the 168 h fit).
Water temperature: per lead, `median + offset + scale * (member - median)` (`tempscore.apply_calibration`), hourly
offsets interpolated the same way, daily highs by lead day.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from flowcast_model.calibrate import (
    ALL_YEARS,
    LEVELS,
    FlowCalibration,
    basin_quantiles,
    climatology_percentile,
)


def _bracket(fitted: np.ndarray, lead: float) -> tuple[float, float, float]:
    """(lower fitted lead, upper fitted lead, weight of the upper) for an hourly lead."""
    if lead <= fitted[0]:
        return fitted[0], fitted[0], 0.0
    if lead >= fitted[-1]:
        return fitted[-1], fitted[-1], 0.0
    j = int(np.searchsorted(fitted, lead))
    lo, hi = fitted[j - 1], fitted[j]
    return lo, hi, 0.0 if hi == lo else (lead - lo) / (hi - lo)


def calibrate_flow(samples_cfs: np.ndarray, basin: str, cal_dir: Path) -> np.ndarray:
    """Calibrated copy of `samples_cfs[lead 1..L, n]` for one issue; unchanged if the basin has no statistics."""
    cal = FlowCalibration.load(cal_dir)
    if basin not in cal.stats.index:
        return samples_cfs
    fold = cal.folds[ALL_YEARS]
    st = cal.stats.loc[basin]
    delta = float(st["delta"])
    fitted = np.sort(fold.params["lead_h"].unique().astype(float))
    x = np.maximum(samples_cfs.astype(float), 0.0)
    z = np.log(x + delta)
    m = np.median(z, axis=1)
    pct = climatology_percentile(np.median(x, axis=1), LEVELS, basin_quantiles(st))
    rb = np.full(len(x), float(st["rb"]))
    params = np.zeros((len(x), 3))
    for i in range(len(x)):
        lo, hi, w = _bracket(fitted, float(i + 1))
        p_lo = fold.lookup(lo, pct[i : i + 1], rb[i : i + 1])[0]
        p_hi = fold.lookup(hi, pct[i : i + 1], rb[i : i + 1])[0] if w else p_lo
        params[i] = (1 - w) * p_lo + w * p_hi
    d = z - m[:, None]
    s = np.where(d > 0, params[:, 2:3], params[:, 1:2])
    return np.maximum(np.exp(m[:, None] + params[:, 0:1] + s * d) - delta, 0.0).astype(np.float32)


def _temp_params(cal: pd.DataFrame, variable: str) -> pd.DataFrame:
    sel = cal[(cal["variable"] == variable) & (cal["wy"] == ALL_YEARS)]
    return sel.sort_values("lead_h")[["lead_h", "offset", "scale"]].reset_index(drop=True)


def calibrate_temperature_hourly(samples: np.ndarray, cal: pd.DataFrame) -> np.ndarray:
    """`samples[lead 1..L, n]` (degC) calibrated per lead, offsets and spread factors interpolated between fitted leads."""
    p = _temp_params(cal, "water_temperature")
    if p.empty:
        return samples
    leads = np.arange(1, samples.shape[0] + 1, dtype=float)
    off = np.interp(leads, p["lead_h"], p["offset"])
    sc = np.interp(leads, p["lead_h"], p["scale"])
    med = np.median(samples, axis=1, keepdims=True)
    return (med + off[:, None] + sc[:, None] * (samples - med)).astype(np.float32)


def calibrate_temperature_daily(maxima: np.ndarray, lead_days: np.ndarray, cal: pd.DataFrame) -> np.ndarray:
    """Daily highs `maxima[day, n]` calibrated with the fit for lead day k (lead_h = 24 k)."""
    p = _temp_params(cal, "water_temperature_daily_max").set_index("lead_h")
    out = maxima.copy()
    for j, k in enumerate(lead_days):
        if 24.0 * k in p.index and np.isfinite(maxima[j]).any():
            row = p.loc[24.0 * k]
            med = np.nanmedian(maxima[j])
            out[j] = med + row["offset"] + row["scale"] * (maxima[j] - med)
    return out
