"""Check schedules and operator-reported releases against the gauge below the dam.

Two checks, both with no fitting to the verification data:
* `hourly_check`: an hourly schedule (cfs, or MW converted at nameplate) vs the gauge, at the travel lag that
  maximizes correlation. Reports correlation, volume bias, MAE, the MAE of persistence (the gauge value at
  each day's local midnight held flat, which is what the model does today without a schedule), and the MAE of
  persistence plus the scheduled change since midnight (which cancels a constant conversion or tributary offset).
* `calendar_check`: season-ahead release days vs whether the gauge shows a release in that window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
from flowcast_pipeline.usgs import Parameter, WaterDataClient

CWMS = "https://cwms-data.usace.army.mil/cwms-data/timeseries"


def cwms_hourly(office: str, name: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """CWMS series as hourly values indexed by hour start (UTC). 'Ave.1Hour' values are stamped at the hour end."""
    resp = requests.get(CWMS, params={"name": name, "office": office, "begin": start.tz_convert("UTC").isoformat(),
                                      "end": end.tz_convert("UTC").isoformat(), "page-size": 20000},
                        headers={"Accept": "application/json;version=2"}, timeout=120)
    resp.raise_for_status()
    vals = [(t, v) for t, v, *_ in resp.json().get("values", []) if v is not None]
    if not vals:
        return pd.Series(dtype=float)
    s = pd.Series([v for _, v in vals], index=pd.to_datetime([t for t, _ in vals], unit="ms", utc=True), dtype=float)
    if ".Ave." in name:
        s.index = s.index - pd.Timedelta(hours=1)
    return s.resample("1h").mean()


def gauge_hourly(client: WaterDataClient, site: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    df = client.continuous(site, Parameter.DISCHARGE, start.tz_convert("UTC").date().isoformat(), (end + pd.Timedelta(days=1)).tz_convert("UTC").date().isoformat())
    if df.empty:
        return pd.Series(dtype=float)
    s = df.set_index("time")["value"].astype(float)
    return s.resample("1h").mean()


@dataclass
class HourlyResult:
    lag_h: int
    r: float
    bias: float
    mae: float
    mae_persistence: float
    mae_persist_delta: float
    n: int


def best_lag(schedule: pd.Series, gauge: pd.Series, max_lag_h: int = 12) -> int:
    best, best_r = 0, -np.inf
    for lag in range(max_lag_h + 1):
        shifted = gauge.shift(-lag).reindex(schedule.index)
        ok = shifted.notna() & schedule.notna()
        if ok.sum() < 12 or schedule[ok].std() == 0:
            continue
        r = np.corrcoef(schedule[ok], shifted[ok])[0, 1]
        if r > best_r:
            best, best_r = lag, r
    return best


def hourly_check(schedule: pd.Series, gauge: pd.Series, tz: str, max_lag_h: int = 12) -> HourlyResult | None:
    """`schedule` hourly (UTC index, value at hour start) in cfs."""
    lag = best_lag(schedule, gauge, max_lag_h)
    obs = gauge.shift(-lag).reindex(schedule.index)
    ok = obs.notna() & schedule.notna()
    if ok.sum() < 12:
        return None
    s, o = schedule[ok], obs[ok]
    local_day = s.index.tz_convert(tz).normalize()
    # Persistence issued at local midnight: the flow then, held for the day (shifted by the same lag).
    first = pd.Series(o.values, index=local_day).groupby(level=0).transform("first").to_numpy()
    s_first = pd.Series(s.values, index=local_day).groupby(level=0).transform("first").to_numpy()
    # How a model would use the schedule: last observation plus the scheduled change since issue.
    delta = first + (s.to_numpy() - s_first)
    r = float(np.corrcoef(s, o)[0, 1]) if s.std() > 0 and o.std() > 0 else float("nan")
    return HourlyResult(
        lag_h=lag, r=r, bias=float(o.sum() / s.sum()) if s.sum() else float("nan"),
        mae=float((s - o).abs().mean()), mae_persistence=float(np.abs(first - o.to_numpy()).mean()),
        mae_persist_delta=float(np.abs(delta - o.to_numpy()).mean()), n=int(ok.sum()),
    )


def calendar_check(days: pd.DataFrame, gauge: pd.Series, tz: str, window: tuple[int, int], threshold_cfs: float, min_rise_cfs: float,
                   first: pd.Timestamp, last: pd.Timestamp) -> pd.DataFrame:
    """Per local day in [first, last]: scheduled (from `days`) vs observed release.

    Observed release: the hourly flow in `window` (local hours, after travel lag) reaches `threshold_cfs` and rises
    at least `min_rise_cfs` above that morning's minimum (so a wet day at high natural flow doesn't count).
    """
    local = gauge.tz_convert(tz)
    scheduled = set(pd.to_datetime(days["valid_start"]).dt.tz_convert(tz).dt.date)
    rows = []
    for day in pd.date_range(first, last, freq="D"):
        d = day.date()
        g = local[str(d)]
        if len(g) < 18:
            continue
        win = g[(g.index.hour >= window[0]) & (g.index.hour < window[1])]
        pre = g[g.index.hour < window[0]]
        observed = bool(len(win) and win.max() >= threshold_cfs and win.max() - pre.min() >= min_rise_cfs)
        rows.append({"date": d, "scheduled": d in scheduled, "observed": observed, "peak_cfs": float(win.max()) if len(win) else np.nan})
    return pd.DataFrame(rows)
