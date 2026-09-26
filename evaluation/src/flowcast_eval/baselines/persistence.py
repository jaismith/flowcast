"""Persistence and recession-persistence (plan milestone 0.5)."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..pairs import ForecastCube


def last_available(obs: pd.Series, times: pd.DatetimeIndex, latency_h: float, max_age_h: float) -> tuple[np.ndarray, np.ndarray]:
    """Latest non-missing obs at or before `time - latency`, and its age at issue time (hours)."""
    s = obs.dropna().sort_index()
    cutoff = times - pd.Timedelta(hours=latency_h)
    pos = s.index.searchsorted(cutoff, side="right") - 1
    ok = pos >= 0
    values = np.full(len(times), np.nan)
    age_h = np.full(len(times), np.nan)
    values[ok] = s.to_numpy()[pos[ok]]
    age_h[ok] = (times[ok] - s.index[pos[ok]]).total_seconds() / 3600.0
    stale = ~ok | (age_h > latency_h + max_age_h)
    values[stale] = np.nan
    return values, age_h


def persistence(
    obs: pd.Series,
    issue_times: pd.DatetimeIndex,
    leads_h,
    site_id: str,
    variable: str = "discharge",
    latency_h: float = 1.0,
    max_age_h: float = 6.0,
) -> ForecastCube:
    """Last observation available at issue time, held for every lead."""
    last, _ = last_available(obs, issue_times, latency_h, max_age_h)
    values = np.repeat(last[:, None], len(leads_h), axis=1)[:, :, None]
    return ForecastCube("persistence", site_id, variable, issue_times, np.asarray(leads_h, float), values)


@dataclass(frozen=True)
class RecessionCurve:
    """Master recession curve -dQ/dt = a * Q^b (Q in ft3/s, t in hours), floored at `q_floor`."""

    a: float
    b: float
    q_floor: float

    def project(self, q0: np.ndarray, hours: np.ndarray) -> np.ndarray:
        """Flow after `hours` ([L] or [N, L]) of recession from `q0` ([N])."""
        q0 = np.asarray(q0, float)[:, None]
        t = np.atleast_2d(np.asarray(hours, float))
        if abs(self.b - 1.0) < 1e-6:
            q = q0 * np.exp(-self.a * t)
        else:
            base = q0 ** (1.0 - self.b) + (self.b - 1.0) * self.a * t
            q = np.where(base > 0, np.abs(base) ** (1.0 / (1.0 - self.b)), self.q_floor)
        return np.maximum(q, np.minimum(self.q_floor, q0))


def fit_recession(obs_train: pd.Series, min_run_days: int = 3) -> RecessionCurve:
    """Fit log(-dQ/dt) = log(a) + b log(Q) on daily means during runs of >= `min_run_days` falling days."""
    daily = obs_train.resample("D").mean().dropna()
    dq = daily.diff().shift(-1)  # change over the next day
    falling = dq < 0
    run_id = (~falling).cumsum()
    run_len = falling.groupby(run_id).transform("sum")
    sel = falling & (run_len >= min_run_days)
    q = daily[sel].to_numpy()
    rate = -dq[sel].to_numpy() / 24.0
    # Robust fit: medians in log-Q bins, then least squares through the bin medians.
    logq, logr = np.log(q), np.log(rate)
    bins = np.quantile(logq, np.linspace(0, 1, 21))
    which = np.clip(np.searchsorted(bins, logq, side="right") - 1, 0, 19)
    mx = np.array([np.median(logq[which == i]) for i in range(20) if (which == i).sum() >= 5])
    my = np.array([np.median(logr[which == i]) for i in range(20) if (which == i).sum() >= 5])
    b, log_a = np.polyfit(mx, my, 1)
    return RecessionCurve(a=float(np.exp(log_a)), b=float(b), q_floor=float(np.quantile(obs_train.dropna(), 0.01)))


def recession_persistence(
    obs: pd.Series,
    issue_times: pd.DatetimeIndex,
    leads_h,
    site_id: str,
    curve: RecessionCurve,
    latency_h: float = 1.0,
    max_age_h: float = 6.0,
    trend_window_h: float = 6.0,
) -> ForecastCube:
    """Persistence, except on a falling limb the last obs follows the fitted recession curve."""
    leads = np.asarray(leads_h, float)
    last, age = last_available(obs, issue_times, latency_h, max_age_h)
    earlier, _ = last_available(obs, issue_times, latency_h + trend_window_h, max_age_h)
    falling = last < earlier
    elapsed = np.nan_to_num(age, nan=latency_h)[:, None] + leads[None, :]
    receding = curve.project(np.nan_to_num(last, nan=1.0), elapsed)
    values = np.where(falling[:, None], receding, last[:, None])
    values[np.isnan(last)] = np.nan
    return ForecastCube("recession_persistence", site_id, "discharge", issue_times, leads, values[:, :, None])
