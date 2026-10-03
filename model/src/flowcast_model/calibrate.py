"""Post-hoc calibration of ensemble hindcasts, fitted on one validation water year and applied to another.

Flow (range stretch). Each issue's ensemble is stretched in log space about its median:

    z = log(x + delta),   z' = m + shift + s_lo * (z - m)  below the median,  m + shift + s_hi * (z - m)  above,

then mapped back and clipped at zero. `delta` is a small per-basin flow (1% of the training-year mean) that keeps
zero flows finite. (shift, s_lo, s_hi) are fitted per lead and per forecast-state cell: the forecast median's
percentile in the basin's training-year hourly-flow climatology (`PCT_EDGES`), crossed with a basin flashiness
class. They minimize the fair CRPS weighted by 1 / (the basin's persistence CRPS at that lead), the same weighting
the cross-basin skill score implies. A cell with too few rows falls back to the forecast-percentile bin pooled over
flashiness, then to the lead's pooled fit. `FlowCalibration` holds one such fit per held-out validation water year
plus one on all of them, with the per-basin statistics it needs; `apply_long` adds the calibrated copy to a
long-format forecast frame.

Water temperature is calibrated in `tempscore`: per lead, `median + offset + scale * (member - median)`.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

PCT_EDGES = (0.5, 0.8, 0.95, 0.99)
SCALES = tuple(np.round(np.arange(0.6, 2.61, 0.1), 2))


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
                rng: np.random.Generator | None = None, free_shift: bool = True) -> tuple[float, float, float]:
    """(shift, s_lo, s_hi) minimizing the weighted mean fair CRPS of `stretch(x, ...)` against `y`. With
    `free_shift=False` the shift is 0, so the ensemble median is kept and only the spread on each side of it is
    fitted."""
    rng = rng or np.random.default_rng(0)
    if len(y) > max_rows:
        pick = rng.choice(len(y), max_rows, replace=False)
        x, y, delta, weight = x[pick], y[pick], delta[pick], weight[pick]
    x = np.asarray(x, float)
    m, d = _log_split(x, np.asarray(delta, float))
    up = d > 0
    w = weight / weight.sum()

    def objective(theta):
        shift, log_lo, log_hi = theta if free_shift else (0.0, *theta)
        s = np.where(up, np.exp(log_hi), np.exp(log_lo))
        xc = np.maximum(np.exp(m[:, None] + shift + s * d) - delta[:, None], 0.0)
        return float(w @ fair_crps_sorted(xc, y))

    res = minimize(objective, np.zeros(3 if free_shift else 2), method="Nelder-Mead", options={"xatol": 2e-3, "fatol": 1e-7, "maxiter": 400})
    shift, log_lo, log_hi = res.x if free_shift else (0.0, *res.x)
    return float(shift), float(np.exp(log_lo)), float(np.exp(log_hi))


@dataclass
class FlowTailCalibration:
    """Per (lead, cell) stretch parameters. Cells: -1 is the lead's pooled fit, `BIN_CELL + b` a forecast-percentile
    bin pooled over flashiness, and `b * n_flash + f` the full cell. Each row uses the most specific fitted cell."""

    flash_edges: tuple[float, ...]
    params: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["lead_h", "cell", "shift", "s_lo", "s_hi", "n"]))
    pct_edges: tuple[float, ...] = PCT_EDGES

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
        return out

    def apply(self, x: np.ndarray, lead_h: float, pct: np.ndarray, rb: np.ndarray, delta: np.ndarray, pooled_only: bool = False) -> np.ndarray:
        p = self.lookup(lead_h, rows=len(x)) if pooled_only else self.lookup(lead_h, pct, rb)
        return stretch(x, delta, p[:, 0], p[:, 1], p[:, 2])

    @classmethod
    def fit(cls, lead_h: float, x: np.ndarray, y: np.ndarray, delta: np.ndarray, weight: np.ndarray, pct: np.ndarray, rb: np.ndarray,
            flash_edges: tuple[float, ...], min_rows: int = 5_000, max_rows: int = 60_000, seed: int = 0, conditional: bool = True,
            free_shift: bool = True) -> FlowTailCalibration:
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
            shift, s_lo, s_hi = fit_stretch(x[idx], y[idx], delta[idx], weight[idx], max_rows, rng, free_shift)
            rows.append({"lead_h": lead_h, "cell": c, "shift": shift, "s_lo": s_lo, "s_hi": s_hi, "n": len(idx)})
        cal.params = pd.DataFrame(rows, columns=["lead_h", "cell", "shift", "s_lo", "s_hi", "n"])
        return cal

    def merge(self, other: FlowTailCalibration) -> FlowTailCalibration:
        assert tuple(other.flash_edges) == tuple(self.flash_edges)
        return FlowTailCalibration(self.flash_edges, pd.concat([self.params, other.params], ignore_index=True), self.pct_edges)


# ---------------------------------------------------------------------------------------------- flow calibration set

LEVELS = np.round(np.concatenate([np.linspace(0, 0.99, 100), np.linspace(0.991, 1.0, 10)]), 3)
ALL_YEARS = 0
CAL_SUFFIX = "_cal"


@dataclass
class FlowCalibration:
    model: str
    folds: dict[int, FlowTailCalibration]
    stats: pd.DataFrame

    @property
    def years(self) -> list[int]:
        return sorted(k for k in self.folds if k != ALL_YEARS)

    def save(self, out: str | Path) -> None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        params = pd.concat([c.params.assign(wy=wy) for wy, c in self.folds.items()], ignore_index=True)
        params[["wy", "lead_h", "cell", "shift", "s_lo", "s_hi", "n"]].to_csv(out / "flow_calibration.csv", index=False)
        any_fold = next(iter(self.folds.values()))
        meta = {"model": self.model, "flash_edges": list(any_fold.flash_edges), "pct_edges": list(any_fold.pct_edges), "levels": LEVELS.tolist()}
        (out / "flow_calibration.json").write_text(json.dumps(meta, indent=1))
        self.stats.to_parquet(out / "basin_stats.parquet")

    @classmethod
    def load(cls, path: str | Path) -> FlowCalibration:
        path = Path(path)
        meta = json.loads((path / "flow_calibration.json").read_text())
        if not np.allclose(meta["levels"], LEVELS):
            raise ValueError("calibration was fitted with other climatology levels")
        params = pd.read_csv(path / "flow_calibration.csv")
        folds = {}
        for wy, p in params.groupby("wy"):
            folds[int(wy)] = FlowTailCalibration(tuple(meta["flash_edges"]), p.drop(columns="wy").reset_index(drop=True), tuple(meta["pct_edges"]))
        return cls(meta["model"], folds, pd.read_parquet(path / "basin_stats.parquet"))


@functools.lru_cache(maxsize=4)
def load_calibration(path: str) -> FlowCalibration:
    return FlowCalibration.load(path)


def basin_quantiles(stats: pd.Series | pd.DataFrame) -> np.ndarray:
    return np.asarray(stats[[f"q{lv:.3f}" for lv in LEVELS]], float)


def apply_long(fc: pd.DataFrame, cal: FlowCalibration, basin: str) -> pd.DataFrame:
    """`fc` plus a calibrated copy (`<model>_cal`) of the calibrated model's rows. Issues in a validation water year
    use the fit that held that year out; other years use the all-years fit. Leads without a fit are left out."""
    basin = basin.removeprefix("USGS-")
    f = fc[fc["model"] == cal.model]
    if f.empty or basin not in cal.stats.index:
        return fc
    st = cal.stats.loc[basin]
    delta = float(st["delta"])
    keys = [f["issue_time"], f["lead_h"]]
    value = f["value"].to_numpy(float)
    pct = climatology_percentile(f["value"].groupby(keys).transform("median").to_numpy(float), LEVELS, basin_quantiles(st))
    z = pd.Series(np.log(np.maximum(value, 0.0) + delta), index=f.index)
    m = z.groupby(keys).transform("median").to_numpy()
    wy = water_year(f["issue_time"])
    fold = np.where(np.isin(wy, cal.years), wy, ALL_YEARS)
    params = np.full((len(f), 3), np.nan)
    lead = f["lead_h"].to_numpy(float)
    for (fw, ld), idx in pd.DataFrame({"fold": fold, "lead": lead}).groupby(["fold", "lead"]).indices.items():
        c = cal.folds.get(int(fw))
        if c is not None and (c.params["lead_h"] == ld).any():
            params[idx] = c.lookup(ld, pct[idx], np.full(len(idx), float(st["rb"])))
    ok = np.isfinite(params[:, 0])
    d = z.to_numpy() - m
    s = np.where(d > 0, params[:, 2], params[:, 1])
    out = f.assign(value=np.maximum(np.exp(m + params[:, 0] + s * d) - delta, 0.0), model=cal.model + CAL_SUFFIX)[ok]
    return pd.concat([fc, out], ignore_index=True)


