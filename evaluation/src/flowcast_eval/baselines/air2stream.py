"""air2stream water-temperature baseline (Toffolon & Piccolroaz 2015, 8-parameter version).

    dTw/dt = [a1 + a2*Ta - a3*Tw + theta*(a5 + a6*cos(2*pi*(t/365 - a7)) - a8*Tw)] / theta**a4,
    theta = Q / mean(Q)

Integrated with a daily explicit step, calibrated on the training years by differential evolution.
flowcast applies it to daily maxima (Tw max vs air Ta max), matching the daily-max temperature target.
In forecast mode the state starts from the last observed day, discharge is persisted from issue time,
and air temperature comes either from observations ("perfect forcing") or from day-of-year climatology
(an operationally fair variant).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from scipy.optimize import differential_evolution
except ImportError:  # the skill-page Lambda ships without scipy (115 MB) and uses a saved fit (`from_dict`)
    differential_evolution = None

from ..pairs import ForecastCube

BOUNDS = [(-5, 5), (0, 2), (0, 2), (-1, 2), (-5, 5), (0, 10), (0, 1), (0, 5)]


def _rate(a: np.ndarray, tw, ta, theta, doy):
    """dTw/dt for parameter array `a` of shape [8, ...] broadcast against the state."""
    delta = theta ** a[3]
    seasonal = a[4] + a[5] * np.cos(2 * np.pi * (doy / 365.0 - a[6])) - a[7] * tw
    return (a[0] + a[1] * ta - a[2] * tw + theta * seasonal) / delta


def simulate(a: np.ndarray, ta: np.ndarray, theta: np.ndarray, doy: np.ndarray, tw0: float | np.ndarray) -> np.ndarray:
    """Free-running daily simulation; `a` is [8] or [8, P] (vectorized over parameter sets)."""
    a = np.asarray(a, float)
    tw = np.broadcast_to(np.asarray(tw0, float), a.shape[1:]).copy() if a.ndim > 1 else np.asarray(tw0, float).copy()
    out = np.empty((len(ta), *tw.shape))
    for i in range(len(ta)):
        tw = np.clip(tw + _rate(a, tw, ta[i], theta[i], doy[i]), 0.0, 40.0)
        out[i] = tw
    return out


@dataclass(frozen=True)
class Air2Stream:
    params: np.ndarray
    q_mean: float
    ta_clim: np.ndarray  # day-of-year mean air temperature, shape [366]
    rmse_train: float

    @staticmethod
    def can_fit() -> bool:
        return differential_evolution is not None

    def to_dict(self) -> dict:
        return {"params": self.params.tolist(), "q_mean": self.q_mean, "ta_clim": self.ta_clim.tolist(), "rmse_train": self.rmse_train}

    @classmethod
    def from_dict(cls, d: dict) -> "Air2Stream":
        return cls(params=np.asarray(d["params"], float), q_mean=float(d["q_mean"]), ta_clim=np.asarray(d["ta_clim"], float), rmse_train=float(d["rmse_train"]))

    @classmethod
    def fit(cls, tw: pd.Series, ta: pd.Series, q: pd.Series, seed: int = 0, maxiter: int = 200) -> "Air2Stream":
        """Calibrate on aligned daily series (index = dates). Tw may have gaps; Ta and Q are interpolated."""
        if differential_evolution is None:
            raise RuntimeError("fitting air2stream needs scipy; load a saved fit with Air2Stream.from_dict instead")
        idx = pd.date_range(max(ta.index.min(), q.index.min()), min(ta.index.max(), q.index.max()), freq="D")
        ta_d = ta.reindex(idx).interpolate(limit_direction="both").to_numpy()
        q_d = q.reindex(idx).interpolate(limit_direction="both").to_numpy()
        tw_d = tw.reindex(idx).to_numpy()
        q_mean = float(np.nanmean(q_d))
        theta, doy = q_d / q_mean, idx.dayofyear.to_numpy()
        ok = ~np.isnan(tw_d)
        tw0 = float(tw_d[ok][0]) if ok.any() else 4.0

        def cost(a):
            sim = simulate(a, ta_d, theta, doy, tw0)
            err = sim[ok] - tw_d[ok, None] if a.ndim > 1 else sim[ok] - tw_d[ok]
            return np.sqrt(np.mean(err**2, axis=0))

        res = differential_evolution(cost, BOUNDS, seed=seed, maxiter=maxiter, popsize=20, tol=1e-6, vectorized=True, updating="deferred", polish=False)
        ta_clim = ta.groupby(ta.index.dayofyear).mean().reindex(range(1, 367)).interpolate(limit_direction="both").to_numpy()
        return cls(params=res.x, q_mean=q_mean, ta_clim=ta_clim, rmse_train=float(res.fun))

    def forecast(
        self,
        tw_obs: pd.Series,
        ta: pd.Series | None,
        q: pd.Series,
        issue_times: pd.DatetimeIndex,
        lead_days,
        site_id: str,
        variable: str = "water_temperature_daily_max",
    ) -> ForecastCube:
        """Forecast days `issue date + lead` from the last observed day before the issue date.

        `ta=None` uses day-of-year climatology for air temperature (operational); otherwise observed Ta.
        """
        leads = np.asarray(lead_days, int)
        issue_days = issue_times.tz_convert(None).floor("D") if issue_times.tz is not None else issue_times.floor("D")
        start = issue_days - pd.Timedelta(days=1)
        tw_obs = _naive(tw_obs)
        q = _naive(q)
        tw0 = tw_obs.reindex(start).to_numpy()
        q0 = q.reindex(start).to_numpy()
        n_steps = int(leads.max()) + 1
        tw = tw0.copy()
        values = np.full((len(issue_times), n_steps), np.nan)
        theta = q0 / self.q_mean
        for k in range(n_steps):
            day = issue_days + pd.Timedelta(days=k)
            doy = day.dayofyear.to_numpy()
            ta_k = self.ta_clim[doy - 1] if ta is None else _naive(ta).reindex(day).to_numpy()
            tw = np.clip(tw + _rate(self.params, tw, ta_k, theta, doy), 0.0, 40.0)
            values[:, k] = tw
        name = "air2stream_clim_air" if ta is None else "air2stream_obs_air"
        run_type = "operational" if ta is None else "perfect_forcing"
        return ForecastCube(name, site_id, variable, issue_times, 24.0 * leads, values[:, leads][:, :, None], run_type=run_type)


def _naive(s: pd.Series) -> pd.Series:
    idx = pd.DatetimeIndex(s.index)
    return s.set_axis(idx.tz_convert(None) if idx.tz is not None else idx)
