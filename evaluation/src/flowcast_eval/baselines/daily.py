"""Daily-variable persistence (e.g. yesterday's daily maximum water temperature)."""

import numpy as np
import pandas as pd

from ..pairs import ForecastCube


def daily_persistence(daily_obs: pd.Series, issue_times: pd.DatetimeIndex, lead_days, site_id: str, variable: str, max_age_days: int = 3) -> ForecastCube:
    """Latest complete day before the issue date, held for every lead day."""
    s = daily_obs.dropna().sort_index()
    idx = pd.DatetimeIndex(s.index)
    idx = idx.tz_convert(None) if idx.tz is not None else idx
    issue_days = issue_times.tz_convert(None).floor("D")
    pos = idx.searchsorted(issue_days - pd.Timedelta(days=1), side="right") - 1
    ok = pos >= 0
    last = np.full(len(issue_times), np.nan)
    last[ok] = s.to_numpy()[pos[ok]]
    age = np.full(len(issue_times), np.inf)
    age[ok] = (issue_days[ok] - idx[pos[ok]]).days
    last[age > max_age_days] = np.nan
    leads = 24.0 * np.asarray(lead_days, float)
    return ForecastCube("persistence", site_id, variable, issue_times, leads, np.repeat(last[:, None], len(leads), axis=1)[:, :, None])
