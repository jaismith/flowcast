"""Day-of-year climatology as a quantile forecast (plan milestone 0.5)."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..pairs import ForecastCube
from ..schema import DAILY_VARIABLES


def _doy(times: pd.DatetimeIndex) -> np.ndarray:
    """Day of year 1..365 with Feb 29 folded onto Feb 28."""
    doy = times.dayofyear.to_numpy()
    leap_after_feb = times.is_leap_year & (doy >= 60)
    return np.where(leap_after_feb, doy - 1, doy)


@dataclass(frozen=True)
class Climatology:
    levels: np.ndarray  # quantile levels, shape [Q]
    table: np.ndarray  # quantiles by day of year, shape [365, Q]

    @classmethod
    def fit(cls, obs_train: pd.Series, n_quantiles: int = 50, window_days: int = 7) -> "Climatology":
        s = obs_train.dropna()
        doy = _doy(s.index)
        values = s.to_numpy()
        levels = (np.arange(n_quantiles) + 0.5) / n_quantiles
        table = np.empty((365, n_quantiles))
        for d in range(1, 366):
            dist = np.abs(doy - d)
            near = np.minimum(dist, 365 - dist) <= window_days
            table[d - 1] = np.quantile(values[near], levels)
        return cls(levels, table)

    def quantiles_at(self, times: pd.DatetimeIndex) -> np.ndarray:
        return self.table[_doy(times) - 1]

    def median_at(self, times: pd.DatetimeIndex) -> np.ndarray:
        return np.array([np.interp(0.5, self.levels, row) for row in self.quantiles_at(times)])


def climatology(clim: Climatology, issue_times: pd.DatetimeIndex, leads_h, site_id: str, variable: str) -> ForecastCube:
    leads = np.asarray(leads_h, float)
    base = issue_times.floor("D") if variable in DAILY_VARIABLES else issue_times
    valid = base.values[:, None] + (leads * 3600 * 1e9).astype("timedelta64[ns]")[None, :]
    q = clim.quantiles_at(pd.DatetimeIndex(valid.ravel()))
    values = q.reshape(len(issue_times), len(leads), len(clim.levels))
    return ForecastCube("climatology", site_id, variable, issue_times, leads, values, kind="quantiles", quantile_levels=clim.levels)
