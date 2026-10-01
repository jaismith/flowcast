"""Post-hoc calibration of ensemble hindcasts, fitted on one validation water year and applied to another.

Flow (upper-tail calibration). Each issue's ensemble is stretched in log space about its median:

    z = log(x + delta),   z' = m + shift + s_lo * (z - m)  below the median,  m + shift + s_hi * (z - m)  above,

then mapped back and clipped at zero. `delta` is a small per-basin flow (1% of the training-year mean) that keeps
zero flows finite. (shift, s_lo, s_hi) are fitted per lead and per forecast-state cell: the forecast median's
percentile in the basin's training-year hourly-flow climatology (`PCT_EDGES`), crossed with a basin flashiness
class. They minimize the fair CRPS weighted by 1 / (the basin's persistence CRPS at that lead), the same weighting
the cross-basin skill score implies. A cell with too few rows falls back to the lead's pooled fit.

Water temperature (daily high). The existing calibration is `median + offset + scale * (member - median)` per lead
day. Here the offset depends on the forecast warm-up `dT` (forecast daily-high air temperature on the target day
minus the issue day's): `offset = a + b_up * max(dT, 0) + b_down * min(dT, 0)`, fitted by least squares on the
residual `obs - median`. The spread factor is then chosen by CRPS on a grid, with the new offset applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize

PCT_EDGES = (0.5, 0.8, 0.95, 0.99)
SCALES = tuple(np.round(np.arange(0.6, 2.61, 0.1), 2))
BOOST_KAPPAS = tuple(np.round(np.arange(1.0, 3.01, 0.1), 2))
WARMUP_CLIP = 12.0


def fair_crps_sorted(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fair (Ferro 2008) CRPS of each row of an ensemble already sorted along axis 1; NaN-free inputs."""
    n = x.shape[1]
    w = (2.0 * np.arange(1, n + 1) - n - 1) / (n * (n - 1))
    return np.abs(x - y[:, None]).mean(axis=1) - x @ w


def water_year(times: pd.DatetimeIndex | pd.Series) -> np.ndarray:
    t = pd.DatetimeIndex(times)
    return (t.year + (t.month >= 10)).to_numpy()


def climatology_percentile(values: np.ndarray, levels: np.ndarray, quantiles: np.ndarray) -> np.ndarray:
    """Non-exceedance probability of `values` in one basin's climatology, given its quantiles at `levels`."""
    q, keep = np.unique(quantiles, return_index=True)
    return np.interp(values, q, levels[keep])


def state_cell(pct: np.ndarray, flash_class: np.ndarray, n_flash: int, edges=PCT_EDGES) -> np.ndarray:
    return np.digitize(pct, edges) * n_flash + np.asarray(flash_class, int)


def _col(a, rows: int) -> np.ndarray:
    return np.broadcast_to(np.asarray(a, float).reshape(-1, 1) if np.ndim(a) else np.full((rows, 1), float(a)), (rows, 1))


def _log_split(x: np.ndarray, delta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = x.shape[1]
    z = np.log(x + delta[:, None])
    m = 0.5 * (z[:, (n - 1) // 2] + z[:, n // 2])
    return m, z - m[:, None]


def stretch(x: np.ndarray, delta: np.ndarray, shift, s_lo, s_hi) -> np.ndarray:
    """Calibrated copy of sorted ensembles `x` [rows, n]; parameters are scalars or per-row arrays. Order is kept."""
    rows = x.shape[0]
    m, d = _log_split(np.asarray(x, float), np.asarray(delta, float))
    s = np.where(d > 0, _col(s_hi, rows), _col(s_lo, rows))
    return np.maximum(np.exp(m[:, None] + _col(shift, rows) + s * d) - np.asarray(delta, float)[:, None], 0.0)


def fit_stretch(x: np.ndarray, y: np.ndarray, delta: np.ndarray, weight: np.ndarray, max_rows: int = 60_000,
                rng: np.random.Generator | None = None, tail: tuple[float, np.ndarray] | None = None) -> tuple[float, float, float]:
    """(shift, s_lo, s_hi) minimizing the weighted mean fair CRPS of `stretch(x, ...)` against `y`.

    With `tail = (lam, threshold)`, adds `lam` times the threshold-weighted CRPS (chaining max(., threshold)),
    which only scores the distribution above each row's threshold."""
    rng = rng or np.random.default_rng(0)
    if len(y) > max_rows:
        pick = rng.choice(len(y), max_rows, replace=False)
        x, y, delta, weight = x[pick], y[pick], delta[pick], weight[pick]
        if tail is not None:
            tail = (tail[0], tail[1][pick])
    x = np.asarray(x, float)
    m, d = _log_split(x, np.asarray(delta, float))
    up = d > 0
    w = weight / weight.sum()

    def objective(theta):
        s = np.where(up, np.exp(theta[2]), np.exp(theta[1]))
        xc = np.maximum(np.exp(m[:, None] + theta[0] + s * d) - delta[:, None], 0.0)
        score = fair_crps_sorted(xc, y)
        if tail is not None:
            thr = tail[1][:, None]
            score = score + tail[0] * fair_crps_sorted(np.maximum(xc, thr), np.maximum(y, tail[1]))
        return float(w @ score)

    res = minimize(objective, np.zeros(3), method="Nelder-Mead", options={"xatol": 2e-3, "fatol": 1e-7, "maxiter": 400})
    return float(res.x[0]), float(np.exp(res.x[1])), float(np.exp(res.x[2]))


@dataclass
class FlowTailCalibration:
    """Per (lead, cell) stretch parameters. Cells: -1 is the lead's pooled fit, `BIN_CELL + b` a forecast-percentile
    bin pooled over flashiness, and `b * n_flash + f` the full cell. Each row uses the most specific fitted cell."""

    flash_edges: tuple[float, ...]
    params: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["lead_h", "cell", "shift", "s_lo", "s_hi", "n"]))
    pct_edges: tuple[float, ...] = PCT_EDGES
    boost: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["lead_h", "from_pct", "kappa"]))

    BIN_CELL = 100

    @property
    def n_flash(self) -> int:
        return len(self.flash_edges) + 1

    def flash_class(self, rb: np.ndarray) -> np.ndarray:
        return np.digitize(rb, self.flash_edges)

    def cells(self, pct: np.ndarray, rb: np.ndarray) -> np.ndarray:
        return state_cell(pct, self.flash_class(rb), self.n_flash, self.pct_edges)

    def lookup(self, lead_h: float, pct: np.ndarray | None = None, rb: np.ndarray | None = None, rows: int | None = None) -> np.ndarray:
        """[rows, 3] parameters for one lead; without `pct` every row gets the lead's pooled fit."""
        p = self.params[self.params["lead_h"] == lead_h].set_index("cell")
        if p.empty:
            raise KeyError(f"no calibration for lead {lead_h} h")
        cols = ["shift", "s_lo", "s_hi"]
        out = np.tile(p.loc[-1, cols].to_numpy(float), (len(pct) if pct is not None else rows, 1))
        if pct is None:
            return out
        pbin = np.digitize(pct, self.pct_edges)
        cell = self.cells(pct, rb)
        for c, row in p.drop(index=-1).iterrows():
            mask = (pbin == c - self.BIN_CELL) if c >= self.BIN_CELL else None
            if mask is not None:
                out[mask] = row[cols].to_numpy(float)
        for c, row in p.drop(index=-1).iterrows():
            if c < self.BIN_CELL:
                out[cell == c] = row[cols].to_numpy(float)
        for b in self.boost[self.boost["lead_h"] == lead_h].itertuples():
            out[pct >= b.from_pct, 2] *= b.kappa
        return out

    def apply(self, x: np.ndarray, lead_h: float, pct: np.ndarray, rb: np.ndarray, delta: np.ndarray, pooled_only: bool = False) -> np.ndarray:
        p = self.lookup(lead_h, rows=len(x)) if pooled_only else self.lookup(lead_h, pct, rb)
        return stretch(x, delta, p[:, 0], p[:, 1], p[:, 2])

    def fit_boost(self, lead_h: float, x: np.ndarray, y: np.ndarray, delta: np.ndarray, pct: np.ndarray, rb: np.ndarray, site: np.ndarray, pers: np.ndarray,
                  budget: float = 0.004, from_pct: float = 0.8, kappas=BOOST_KAPPAS) -> pd.DataFrame:
        """Deliberately widen the upper tail beyond the CRPS fit where the forecast is high: multiply s_hi by the
        largest kappa whose per-basin CRPS skill vs persistence (`pers` = persistence forecast) drops by at most
        `budget`, in both the median and the mean over basins. Returns the kappa scan; stores the choice."""
        self.boost = self.boost[self.boost["lead_h"] != lead_h]
        hit = np.flatnonzero(pct >= from_pct)
        sites, inv = np.unique(site, return_inverse=True)
        p_sum = np.bincount(inv, np.abs(pers - y), minlength=len(sites))
        base = self.lookup(lead_h, pct[hit], rb[hit])
        xs = np.asarray(x[hit], float)
        c0 = fair_crps_sorted(stretch(xs, delta[hit], base[:, 0], base[:, 1], base[:, 2]), y[hit])
        scan = []
        for k in kappas:
            ck = fair_crps_sorted(stretch(xs, delta[hit], base[:, 0], base[:, 1], base[:, 2] * k), y[hit])
            loss = np.bincount(inv[hit], ck - c0, minlength=len(sites)) / np.maximum(p_sum, 1e-12)
            scan.append({"kappa": k, "median_loss": float(np.median(loss)), "mean_loss": float(loss.mean())})
        scan = pd.DataFrame(scan)
        ok = scan[(scan["median_loss"] <= budget) & (scan["mean_loss"] <= budget)]
        kappa = float(ok["kappa"].max()) if len(ok) else 1.0
        self.boost = pd.concat([self.boost, pd.DataFrame([{"lead_h": lead_h, "from_pct": from_pct, "kappa": kappa}])], ignore_index=True)
        return scan

    @classmethod
    def fit(cls, lead_h: float, x: np.ndarray, y: np.ndarray, delta: np.ndarray, weight: np.ndarray, pct: np.ndarray, rb: np.ndarray,
            flash_edges: tuple[float, ...], min_rows: int = 5_000, max_rows: int = 60_000, seed: int = 0, tail_lam: float = 0.0,
            tail_threshold: np.ndarray | None = None, conditional: bool = True) -> FlowTailCalibration:
        cal = cls(tuple(flash_edges))
        rng = np.random.default_rng(seed)
        groups = [(-1, np.arange(len(y)))]
        if conditional:
            pbin = np.digitize(pct, cal.pct_edges)
            cell = cal.cells(pct, rb)
            groups += [(cls.BIN_CELL + int(b), np.flatnonzero(pbin == b)) for b in np.unique(pbin)]
            groups += [(int(c), np.flatnonzero(cell == c)) for c in np.unique(cell)]
        rows = []
        for c, idx in groups:
            if len(idx) < min_rows:
                continue
            tail = (tail_lam, tail_threshold[idx]) if tail_lam > 0 else None
            shift, s_lo, s_hi = fit_stretch(x[idx], y[idx], delta[idx], weight[idx], max_rows, rng, tail)
            rows.append({"lead_h": lead_h, "cell": c, "shift": shift, "s_lo": s_lo, "s_hi": s_hi, "n": len(idx)})
        cal.params = pd.DataFrame(rows, columns=["lead_h", "cell", "shift", "s_lo", "s_hi", "n"])
        return cal

    def merge(self, other: FlowTailCalibration) -> FlowTailCalibration:
        assert tuple(other.flash_edges) == tuple(self.flash_edges)
        return FlowTailCalibration(self.flash_edges, pd.concat([self.params, other.params], ignore_index=True), self.pct_edges,
                                   pd.concat([self.boost, other.boost], ignore_index=True))


def warmup_design(dt: np.ndarray) -> np.ndarray:
    d = np.clip(np.nan_to_num(np.asarray(dt, float)), -WARMUP_CLIP, WARMUP_CLIP)
    return np.column_stack([np.ones_like(d), np.maximum(d, 0.0), np.minimum(d, 0.0)])


@dataclass
class TempWarmupCalibration:
    """Per (group, lead day): offset coefficients (a, b_up, b_down) on the forecast warm-up, and a spread factor.
    With `warmup=False` the slopes are zero, which is the existing lead-only calibration. `group` is an optional
    per-row label (for example regulated vs not); without it every row is group 0."""

    table: pd.DataFrame

    def apply(self, members: np.ndarray, lead_day: np.ndarray, dt: np.ndarray, group: np.ndarray | None = None) -> np.ndarray:
        group = np.zeros(len(members), int) if group is None else np.asarray(group)
        t = self.table.set_index(["group", "lead_day"]).reindex(pd.MultiIndex.from_arrays([group, lead_day]))
        if t["a"].isna().any():
            raise KeyError("group or lead day without calibration")
        med = np.median(members, axis=1)
        offset = (warmup_design(dt) * t[["a", "b_up", "b_down"]].to_numpy(float)).sum(axis=1)
        return (med + offset)[:, None] + t["scale"].to_numpy(float)[:, None] * (members - med[:, None])

    @classmethod
    def fit(cls, members: np.ndarray, obs: np.ndarray, lead_day: np.ndarray, dt: np.ndarray, warmup: bool = True,
            group: np.ndarray | None = None, scales=SCALES) -> TempWarmupCalibration:
        group = np.zeros(len(members), int) if group is None else np.asarray(group)
        med = np.median(members, axis=1)
        rows = []
        for g in np.unique(group):
            for k in np.unique(lead_day):
                i = np.flatnonzero((group == g) & (lead_day == k) & np.isfinite(obs))
                if len(i) == 0:
                    continue
                resid = obs[i] - med[i]
                coef = np.linalg.lstsq(warmup_design(dt[i]), resid, rcond=None)[0] if warmup else np.array([resid.mean(), 0.0, 0.0])
                offset = warmup_design(dt[i]) @ coef
                dev = members[i] - med[i, None]
                crps = {s: float(fair_crps_sorted(np.sort(med[i, None] + offset[:, None] + s * dev, axis=1), obs[i]).mean()) for s in scales}
                rows.append({"group": g, "lead_day": int(k), "a": coef[0], "b_up": coef[1], "b_down": coef[2], "scale": min(crps, key=crps.get), "n": len(i)})
        return cls(pd.DataFrame(rows))
