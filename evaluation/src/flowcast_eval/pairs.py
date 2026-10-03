"""Forecast/observation pairs: the common currency of the harness.

Every forecast source is reduced to one row per (model, issue_time, lead) with a point value
(the median for ensembles and quantile sets), its CRPS, an 80% interval and the verifying observation.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import crps_ensemble, crps_quantiles, ensemble_summary, quantile_summary
from .protocol import lead_bin
from .schema import DAILY_VARIABLES

PAIR_COLUMNS = ["model", "site_id", "variable", "run_type", "issue_time", "valid_time", "lead_h", "point", "crps", "lo", "hi", "obs"]


@dataclass
class ForecastCube:
    """Dense forecasts `values[issue, lead, member]` on a regular lead grid (member axis has size 1 if deterministic)."""

    model: str
    site_id: str
    variable: str
    issue_times: pd.DatetimeIndex
    leads_h: np.ndarray
    values: np.ndarray
    kind: str = "deterministic"  # deterministic | ensemble | quantiles
    quantile_levels: np.ndarray | None = None
    run_type: str = "operational"

    def valid_times(self) -> np.ndarray:
        base = self.issue_times.floor("D") if self.variable in DAILY_VARIABLES else self.issue_times
        return base.values[:, None] + (np.asarray(self.leads_h) * 3600 * 1e9).astype("timedelta64[ns]")[None, :]

    def to_long(self) -> pd.DataFrame:
        n, l, m = self.values.shape
        valid = self.valid_times()
        frame = pd.DataFrame(
            {
                "site_id": self.site_id,
                "variable": self.variable,
                "model": self.model,
                "issue_time": np.repeat(np.repeat(self.issue_times.values, l), m),
                "valid_time": np.repeat(valid.ravel(), m),
                "lead_h": np.tile(np.repeat(np.asarray(self.leads_h, float), m), n),
                "value": self.values.ravel(),
                "run_type": self.run_type,
            }
        )
        frame["issue_time"] = frame["issue_time"].dt.tz_localize("UTC")
        frame["valid_time"] = frame["valid_time"].dt.tz_localize("UTC")
        if self.kind == "ensemble":
            frame["member"] = np.tile(np.arange(m), n * l)
        elif self.kind == "quantiles":
            frame["quantile"] = np.tile(np.asarray(self.quantile_levels, float), n * l)
        return frame.dropna(subset=["value"]).reset_index(drop=True)


def lookup_obs(obs: pd.Series, times, tolerance: str = "30min") -> np.ndarray:
    """Observation at each time (nearest within `tolerance`), NaN if none."""
    obs = obs[~obs.index.duplicated()].sort_index().dropna()
    idx = pd.DatetimeIndex(times)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return obs.reindex(idx, method="nearest", tolerance=pd.Timedelta(tolerance)).to_numpy(float)


def interpolate_series(series: pd.Series, times, max_gap: str = "1h") -> np.ndarray:
    """Value of a continuous series at each time, linear in time between the neighbouring values; NaN outside the
    series or where those neighbours are more than `max_gap` apart."""
    s = series[~series.index.duplicated()].sort_index().dropna()
    idx = pd.DatetimeIndex(times)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    out = np.full(len(idx), np.nan)
    if s.empty:
        return out
    x, y, t = s.index.as_unit("ns").asi8, s.to_numpy(float), idx.as_unit("ns").asi8
    hi = np.searchsorted(x, t, side="left")
    exact = (hi < len(x)) & (x[np.minimum(hi, len(x) - 1)] == t)
    out[exact] = y[hi[exact]]
    inner = ~exact & (hi > 0) & (hi < len(x))
    lo, hi = hi[inner] - 1, hi[inner]
    gap = x[hi] - x[lo]
    w = (t[inner] - x[lo]) / gap
    out[inner] = np.where(gap <= pd.Timedelta(max_gap).value, y[lo] + w * (y[hi] - y[lo]), np.nan)
    return out


def _summaries(kind: str, values: np.ndarray, obs: np.ndarray, levels: np.ndarray | None):
    if kind == "ensemble":
        point, lo, hi = ensemble_summary(values)
        crps = crps_ensemble(values, obs)
    elif kind == "quantiles":
        point, lo, hi = quantile_summary(values, levels)
        crps = crps_quantiles(values, levels, obs)
    else:
        point = values[:, 0]
        lo = hi = np.full(len(point), np.nan)
        crps = np.abs(point - obs)
    return point, crps, lo, hi


def pairs_from_cube(cube: ForecastCube, obs: pd.Series) -> pd.DataFrame:
    n, l, m = cube.values.shape
    valid = pd.DatetimeIndex(cube.valid_times().ravel()).tz_localize("UTC")
    o = lookup_obs(obs, valid)
    point, crps, lo, hi = _summaries(cube.kind, cube.values.reshape(n * l, m), o, cube.quantile_levels)
    return pd.DataFrame(
        {
            "model": cube.model,
            "site_id": cube.site_id,
            "variable": cube.variable,
            "run_type": cube.run_type,
            "issue_time": np.repeat(cube.issue_times, l),
            "valid_time": valid,
            "lead_h": np.tile(np.asarray(cube.leads_h, float), n),
            "point": point,
            "crps": crps,
            "lo": lo,
            "hi": hi,
            "obs": o,
        }
    )


def pairs_from_long(forecasts: pd.DataFrame, obs: pd.Series, leads_h: tuple[float, ...]) -> pd.DataFrame:
    """Pairs from normalized long-format forecasts (see `schema`). Leads are binned onto `leads_h`."""
    out = []
    keys = ["model", "site_id", "variable", "run_type"]
    for (model, site, variable, run_type), g in forecasts.groupby(keys, sort=False):
        g = g.assign(lead_bin=lead_bin(g["lead_h"], leads_h).to_numpy()).dropna(subset=["lead_bin", "value"])
        if g.empty:
            continue
        # Several raw leads in one bin (irregular issue times): keep the one closest to the bin lead.
        g = g.sort_values("lead_h")
        if g["member"].notna().any():
            kind, col, levels = "ensemble", "member", None
        elif g["quantile"].notna().any():
            kind, col = "quantiles", "quantile"
            levels = np.sort(g["quantile"].dropna().unique())
        else:
            kind, col, levels = "deterministic", None, None
        index = ["issue_time", "lead_bin"]
        chosen = g.drop_duplicates(index, keep="last").set_index(index)["valid_time"]
        if col is None:
            wide = g.drop_duplicates(index, keep="last").set_index(index)[["value"]]
        else:
            same_valid = chosen.reindex(pd.MultiIndex.from_frame(g[index])).to_numpy() == g["valid_time"].to_numpy()
            g = g[same_valid].drop_duplicates([*index, col], keep="last")
            wide = g.set_index([*index, col])["value"].unstack(col)
            if kind == "quantiles":
                wide = wide.reindex(columns=levels)
        valid = chosen.reindex(wide.index)
        o = lookup_obs(obs, valid.to_numpy())
        point, crps, lo, hi = _summaries(kind, wide.to_numpy(float), o, levels)
        out.append(
            pd.DataFrame(
                {
                    "model": model,
                    "site_id": site,
                    "variable": variable,
                    "run_type": run_type,
                    "issue_time": wide.index.get_level_values("issue_time"),
                    "valid_time": valid.to_numpy(),
                    "lead_h": wide.index.get_level_values("lead_bin").astype(float),
                    "point": point,
                    "crps": crps,
                    "lo": lo,
                    "hi": hi,
                    "obs": o,
                }
            )
        )
    if not out:
        return pd.DataFrame(columns=PAIR_COLUMNS)
    pairs = pd.concat(out, ignore_index=True)
    pairs["valid_time"] = pd.to_datetime(pairs["valid_time"], utc=True)
    return pairs[PAIR_COLUMNS]


def align_issue_times(pairs: pd.DataFrame, models: list[str] | None = None) -> pd.DataFrame:
    """Keep only (issue_time, lead_h) combinations verified for every model that forecasts that lead (plan §8.3 rule 1).

    A model that never issues a given lead (e.g. a 3-hourly ensemble at lead 1 h) doesn't restrict that lead.
    """
    models = models or sorted(pairs["model"].unique())
    p = pairs[pairs["model"].isin(models) & pairs["point"].notna() & pairs["obs"].notna()]
    counts = p.groupby(["issue_time", "lead_h"])["model"].nunique()
    needed = p.groupby("lead_h")["model"].nunique().reindex(counts.index.get_level_values("lead_h")).to_numpy()
    common = counts.index[counts.to_numpy() == needed]
    keep = pd.MultiIndex.from_frame(p[["issue_time", "lead_h"]]).isin(common)
    return p[keep].reset_index(drop=True)
