"""Strong reference baselines: upstream routing, ARX and gradient-boosted trees (stronger than persistence).

All three are fitted on the training years (WY2001-2019) and scored only on validation years, so the frozen
test stays untouched. Every forecast uses only what is known at issue time: observations lag 1 h, upstream
gauges beyond the last observation are held at their last value, and future precipitation is either

* `qpf`: the archived GEFS ensemble-mean forecast from the latest init available at issue time (fair), or
* `obs_precip`: observed (ERA5) precipitation standing in for the forecast. This is perfect forcing and
  optimistic; it bounds what a better precipitation forecast could buy.

The ARX and tree models are fitted with observed precipitation in the future windows ("perfect prog") and
applied with either source, because no archived forecast precipitation covers the training years.

* `routing_upstream`: per-lead non-negative weights on routed changes at the upstream and below-dam gauges. Each
  gauge's travel time and attenuation (a trailing moving average) are fitted on training-year forecast error.
* `arx_*`: per-lead ridge regression of the log-flow change on recent site flow, upstream gauges, past and
  future basin precipitation and season.
* `lgbm_*`: per-lead LightGBM (L1 objective) on the same features.

All are deterministic, so their CRPS equals MAE.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import nnls

from flowcast_pipeline.lake import Lake
from flowcast_pipeline.obs import obs_series
from flowcast_pipeline.sites import Site, get_site

from . import nwm
from .archive import read_archive
from .pairs import ForecastCube, pairs_from_cube, pairs_from_long
from .precip import GEFS_START, era5_basin_precip, forecast_precip, gefs_basin_qpf, observed_precip_windows
from .protocol import VALIDATION, HindcastProtocol
from .scoreboard import _cube_from_series, _md, _skill_table, discharge_baselines, table
from .scoring import score_pairs

log = logging.getLogger(__name__)

# Validation years with archived GEFS forecasts (Oct 2020 onward): WY2021-2022.
STRONG_VALIDATION = VALIDATION.with_window("2020-10-01", "2022-09-30T23:00", name="validation-wy2021-2022-gefs")
MARFC_MAX_LEAD_H = 72.0
MAX_AGE_H = 6
PAST_PRECIP_H = (6, 24, 72, 168)
FUTURE_TRIMS_H = (0, 12, 24)
STRONG_MODELS = ["routing_upstream", "arx_qpf", "arx_obs_precip", "lgbm_qpf", "lgbm_obs_precip"]
MODEL_NOTES = {
    "routing_upstream": "upstream and below-dam gauges, fitted travel times; no precipitation",
    "arx_qpf": "linear ARX with archived GEFS QPF",
    "arx_obs_precip": "linear ARX with observed precipitation as the forecast (perfect forcing, optimistic)",
    "lgbm_qpf": "LightGBM per lead with archived GEFS QPF",
    "lgbm_obs_precip": "LightGBM per lead with observed precipitation as the forecast (perfect forcing, optimistic)",
    "nwm_retrospective": "NWM v3.0 retrospective: AORC-forced simulation, no data assimilation",
}


@dataclass
class Inputs:
    """Hourly inputs on one UTC grid. Observation arrays are forward-filled up to MAX_AGE_H (past only)."""

    site: Site
    q: pd.Series
    upstream: dict[str, pd.Series]
    precip: pd.Series
    qpf: pd.DataFrame
    index: pd.DatetimeIndex = field(init=False)
    _q_raw: np.ndarray = field(init=False)
    _arrays: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self):
        start = min(s.dropna().index.min() for s in [self.q, *self.upstream.values()])
        end = max(s.dropna().index.max() for s in [self.q, *self.upstream.values()])
        self.index = pd.date_range(start.floor("h"), end.ceil("h") + pd.Timedelta(days=8), freq="h")
        self._q_raw = self.q.reindex(self.index).to_numpy(float)
        self._arrays = {"q": self.q.reindex(self.index).ffill(limit=MAX_AGE_H).to_numpy(float)}
        for gauge, series in self.upstream.items():
            self._arrays[gauge] = series.reindex(self.index).ffill(limit=MAX_AGE_H).to_numpy(float)

    def at(self, name: str, times: pd.DatetimeIndex) -> np.ndarray:
        """Hourly value at each time, floored to the hour (the latest reading on the hourly grid)."""
        pos = np.floor(((times - self.index[0]) / pd.Timedelta(hours=1)).to_numpy())
        out = np.full(len(times), np.nan)
        ok = (pos >= 0) & (pos < len(self.index))
        arr = self._q_raw if name == "q_raw" else self._arrays[name]
        out[ok] = arr[pos[ok].astype(int)]
        return out


def _log(x: np.ndarray) -> np.ndarray:
    return np.log(np.maximum(x, 1.0))


def features(inp: Inputs, issues: pd.DatetimeIndex, lead_h: float, precip_mode: str, latency_h: float = 1.0) -> pd.DataFrame:
    """Predictors known at issue time for one lead. `precip_mode` is `obs` (perfect) or `qpf` (GEFS)."""
    a = issues - pd.Timedelta(hours=latency_h)
    lq0 = _log(inp.at("q", a))
    cols: dict[str, np.ndarray] = {"lq0": lq0}
    for h in (3, 6, 24):
        cols[f"dq{h}"] = lq0 - _log(inp.at("q", a - pd.Timedelta(hours=h)))
    for gauge in inp.upstream:
        lu = _log(inp.at(gauge, a))
        cols[f"{gauge}_rel"] = lu - lq0
        for h in (6, 24):
            cols[f"{gauge}_d{h}"] = lu - _log(inp.at(gauge, a - pd.Timedelta(hours=h)))
    past = observed_precip_windows(inp.precip, issues, [(-latency_h - h, -latency_h) for h in PAST_PRECIP_H])
    for k, h in enumerate(PAST_PRECIP_H):
        cols[f"p_past{h}"] = past[:, k]
    windows = [(-latency_h, max(-latency_h, lead_h - trim)) for trim in FUTURE_TRIMS_H]
    future = observed_precip_windows(inp.precip, issues, windows) if precip_mode == "obs" else forecast_precip(inp.qpf, issues, windows)
    for k, trim in enumerate(FUTURE_TRIMS_H):
        cols[f"p_next_minus{trim}"] = future[:, k]
    doy = issues.dayofyear.to_numpy()
    cols["doy_sin"], cols["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
    return pd.DataFrame(cols, index=issues)


def target(inp: Inputs, issues: pd.DatetimeIndex, lead_h: float, latency_h: float = 1.0) -> np.ndarray:
    """Log-flow change from the last observation to the verifying hour."""
    return _log(inp.at("q_raw", issues + pd.Timedelta(hours=lead_h))) - _log(inp.at("q", issues - pd.Timedelta(hours=latency_h)))


def _cube(name: str, inp: Inputs, issues: pd.DatetimeIndex, leads, values: np.ndarray, run_type: str) -> ForecastCube:
    return ForecastCube(name, inp.site.id, "discharge", issues, np.asarray(leads, float), values[:, :, None], run_type=run_type)


# ---------------------------------------------------------------- (a) routing


@dataclass
class Routing:
    travel_h: dict[str, int]
    window_h: dict[str, int]
    weights: dict[float, dict[str, float]]

    @staticmethod
    def _smoothed(inp: Inputs, gauge: str, window: int) -> pd.Series:
        s = pd.Series(inp._arrays[gauge], index=inp.index)
        return s.rolling(window, min_periods=max(1, window // 2)).mean()

    @classmethod
    def fit(cls, inp: Inputs, train: pd.DatetimeIndex, leads, latency_h: float = 1.0, fit_leads_h=(6.0, 12.0, 24.0), sweeps: int = 2) -> Routing:
        """Travel time and attenuation per gauge by coordinate descent on training-year forecast error.

        Lag correlation doesn't identify travel times here: rain reaches upstream and downstream gauges at about
        the same time, so contemporaneous changes dominate. Instead each gauge's (tau, window) is chosen to
        minimize the summed squared error of the per-lead non-negative fits at `fit_leads_h`, holding the others.
        """
        model = cls({g: 6 for g in inp.upstream}, {g: 1 for g in inp.upstream}, {})
        smoothed = {(g, w): cls._smoothed(inp, g, w) for g in inp.upstream for w in (1, 3, 6, 12)}
        targets = {lead: inp.at("q_raw", train + pd.Timedelta(hours=lead)) - inp.at("q", train - pd.Timedelta(hours=latency_h)) for lead in fit_leads_h}

        def sse() -> float:
            total = 0.0
            for lead, y in targets.items():
                x = model._design(inp, train, lead, latency_h, smoothed)
                ok = np.isfinite(y) & np.isfinite(x).all(axis=1)
                _, resid = nnls(x[ok], y[ok])
                total += resid**2 / ok.sum()
            return total

        for _ in range(sweeps):
            for gauge in inp.upstream:
                best = (np.inf, model.travel_h[gauge], model.window_h[gauge])
                for w in (1, 3, 6, 12):
                    for tau in range(0, 37, 1):
                        model.travel_h[gauge], model.window_h[gauge] = tau, w
                        err = sse()
                        if err < best[0]:
                            best = (err, tau, w)
                model.travel_h[gauge], model.window_h[gauge] = best[1], best[2]
                log.info("routing %s: travel %d h, window %d h", gauge, best[1], best[2])
        for lead in leads:
            x = model._design(inp, train, lead, latency_h)
            y = inp.at("q_raw", train + pd.Timedelta(hours=lead)) - inp.at("q", train - pd.Timedelta(hours=latency_h))
            ok = np.isfinite(y) & np.isfinite(x).all(axis=1)
            coef, _ = nnls(x[ok], y[ok])
            model.weights[float(lead)] = dict(zip(inp.upstream, coef.tolist()))
        return model

    def _design(self, inp: Inputs, issues: pd.DatetimeIndex, lead: float, latency_h: float, smoothed: dict | None = None) -> np.ndarray:
        a = (issues - pd.Timedelta(hours=latency_h)).floor("h")
        cols = []
        for gauge in inp.upstream:
            key = (gauge, self.window_h[gauge])
            s = smoothed[key] if smoothed is not None and key in smoothed else self._smoothed(inp, gauge, self.window_h[gauge])
            tau = pd.Timedelta(hours=self.travel_h[gauge])
            arrive = (issues + pd.Timedelta(hours=lead) - tau).floor("h")
            now = s.reindex(arrive.where(arrive <= a, a)).to_numpy()
            then = s.reindex(a - tau).to_numpy()
            cols.append(now - then)
        return np.column_stack(cols)

    def forecast(self, inp: Inputs, issues: pd.DatetimeIndex, leads, latency_h: float = 1.0) -> ForecastCube:
        q0 = inp.at("q", issues - pd.Timedelta(hours=latency_h))
        values = np.full((len(issues), len(leads)), np.nan)
        for j, lead in enumerate(leads):
            w = np.array([self.weights[float(lead)][g] for g in inp.upstream])
            values[:, j] = np.maximum(q0 + self._design(inp, issues, lead, latency_h) @ w, 0.0)
        return _cube("routing_upstream", inp, issues, leads, values, "operational")


# ---------------------------------------------------------------- (b) ARX


@dataclass
class ARX:
    columns: dict[float, list[str]]
    mean: dict[float, np.ndarray]
    scale: dict[float, np.ndarray]
    coef: dict[float, np.ndarray]
    alpha: float = 1.0

    @classmethod
    def fit(cls, inp: Inputs, train: pd.DatetimeIndex, leads, alpha: float = 1.0) -> ARX:
        model = cls({}, {}, {}, {}, alpha)
        for lead in leads:
            x = features(inp, train, lead, "obs")
            x = x.loc[:, x.notna().mean() > 0.5]
            y = target(inp, train, lead)
            ok = np.isfinite(y) & x.notna().all(axis=1).to_numpy()
            xm = x.to_numpy()[ok]
            mean, scale = xm.mean(axis=0), xm.std(axis=0) + 1e-9
            z = np.column_stack([np.ones(ok.sum()), (xm - mean) / scale])
            reg = alpha * np.eye(z.shape[1])
            reg[0, 0] = 0.0
            coef = np.linalg.solve(z.T @ z + reg, z.T @ y[ok])
            model.columns[float(lead)], model.mean[float(lead)], model.scale[float(lead)], model.coef[float(lead)] = list(x.columns), mean, scale, coef
        return model

    def forecast(self, inp: Inputs, issues: pd.DatetimeIndex, leads, precip_mode: str) -> ForecastCube:
        lq0 = _log(inp.at("q", issues - pd.Timedelta(hours=1)))
        values = np.full((len(issues), len(leads)), np.nan)
        for j, lead in enumerate(leads):
            x = features(inp, issues, lead, precip_mode)[self.columns[float(lead)]].to_numpy()
            z = np.column_stack([np.ones(len(x)), (x - self.mean[float(lead)]) / self.scale[float(lead)]])
            values[:, j] = np.exp(lq0 + z @ self.coef[float(lead)])
        name = f"arx_{'qpf' if precip_mode == 'qpf' else 'obs_precip'}"
        return _cube(name, inp, issues, leads, values, "operational" if precip_mode == "qpf" else "perfect_forcing")


# ---------------------------------------------------------------- (c) LightGBM


@dataclass
class GBM:
    boosters: dict[float, lgb.Booster]
    columns: list[str]

    PARAMS = {
        "objective": "l1", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 50,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
        "verbosity": -1, "seed": 20260926, "num_threads": 8,
    }

    @classmethod
    def fit(cls, inp: Inputs, train: pd.DatetimeIndex, leads, holdout_start: str = "2017-10-01") -> GBM:
        """Early stopping on the last two training water years; validation years are never seen."""
        boosters, columns = {}, []
        cutoff = pd.Timestamp(holdout_start, tz="UTC")
        for lead in leads:
            x = features(inp, train, lead, "obs")
            y = target(inp, train, lead)
            ok = np.isfinite(y)
            fit_rows, stop_rows = ok & (train < cutoff), ok & (train >= cutoff)
            dtrain = lgb.Dataset(x[fit_rows], y[fit_rows])
            dstop = lgb.Dataset(x[stop_rows], y[stop_rows], reference=dtrain)
            boosters[float(lead)] = lgb.train(cls.PARAMS, dtrain, num_boost_round=1500, valid_sets=[dstop], callbacks=[lgb.early_stopping(100, verbose=False)])
            columns = list(x.columns)
        return cls(boosters, columns)

    def forecast(self, inp: Inputs, issues: pd.DatetimeIndex, leads, precip_mode: str) -> ForecastCube:
        lq0 = _log(inp.at("q", issues - pd.Timedelta(hours=1)))
        values = np.full((len(issues), len(leads)), np.nan)
        for j, lead in enumerate(leads):
            x = features(inp, issues, lead, precip_mode)[self.columns]
            b = self.boosters[float(lead)]
            values[:, j] = np.exp(lq0 + b.predict(x, num_iteration=b.best_iteration))
        values[np.isnan(lq0)] = np.nan
        if precip_mode == "qpf":
            no_qpf = features(inp, issues, float(leads[-1]), "qpf")["p_next_minus0"].isna().to_numpy()
            values[no_qpf] = np.nan
        name = f"lgbm_{'qpf' if precip_mode == 'qpf' else 'obs_precip'}"
        return _cube(name, inp, issues, leads, values, "operational" if precip_mode == "qpf" else "perfect_forcing")


# ---------------------------------------------------------------- scoring


@dataclass
class StrongModels:
    routing: Routing
    arx: ARX
    gbm: GBM

    def cubes(self, inp: Inputs, issues: pd.DatetimeIndex, leads) -> list[ForecastCube]:
        return [
            self.routing.forecast(inp, issues, leads),
            self.arx.forecast(inp, issues, leads, "qpf"),
            self.arx.forecast(inp, issues, leads, "obs"),
            self.gbm.forecast(inp, issues, leads, "qpf"),
            self.gbm.forecast(inp, issues, leads, "obs"),
        ]


def load_inputs(lake: Lake, site: Site, end: pd.Timestamp) -> Inputs:
    q = obs_series(lake, site.id, "discharge")
    upstream = {g: obs_series(lake, g, "discharge") for g in (*site.upstream_gauges, *site.regulation_gauges)}
    upstream = {g: s for g, s in upstream.items() if not s.dropna().empty}
    precip = era5_basin_precip(site.id, "2000-01-01", f"{end + pd.Timedelta(days=10):%Y-%m-%d}")
    qpf = gefs_basin_qpf(site.id, GEFS_START, end + pd.Timedelta(days=1))
    return Inputs(site, q, upstream, precip, qpf)


def fit_models(inp: Inputs, protocol: HindcastProtocol) -> StrongModels:
    train = _issues(protocol.train_window, protocol)
    leads = protocol.leads_h
    log.info("fitting on %d training issues", len(train))
    return StrongModels(Routing.fit(inp, train, leads), ARX.fit(inp, train, leads), GBM.fit(inp, train, leads))


def _issues(window: tuple[pd.Timestamp, pd.Timestamp], protocol: HindcastProtocol) -> pd.DatetimeIndex:
    hours = pd.date_range(window[0].floor("D"), window[1], freq="h")
    return hours[hours.hour.isin(protocol.issue_hours_utc)]


def score_strong(lake: Lake, archive: Lake, site_id: str, n_boot: int = 1000, protocol: HindcastProtocol = STRONG_VALIDATION) -> dict:
    site = get_site(site_id)
    protocol = replace(protocol, n_boot=n_boot)
    start, end = protocol.test_window
    inp = load_inputs(lake, site, end)
    models = fit_models(inp, protocol)
    leads = protocol.leads_h
    q = inp.q
    retro = nwm.retrospective(site.nwm_reach, start="2019-10-01", end="2023-02-01") if site.nwm_reach else None

    def all_cubes(issues: pd.DatetimeIndex) -> list[ForecastCube]:
        cubes = [*discharge_baselines(q, site, protocol, issues), *models.cubes(inp, issues, leads)]
        if retro is not None:
            cubes.append(_cube_from_series("nwm_retrospective", retro, site, issues, leads, "simulation"))
        return cubes

    issues = protocol.issue_times(until=q.index.max())
    pairs = pd.concat([pairs_from_cube(c, q) for c in all_cubes(issues)], ignore_index=True)
    scores, vs_persistence = score_pairs(pairs, protocol, reference="persistence")

    # Best opponent: MARFC at its own issue times through 72 h, the NWM (the only one covering these years is the
    # retrospective simulation) beyond. Every other model is issued at exactly MARFC's times.
    marfc = read_archive(archive, ["marfc_rvf"], site.id, since=start)
    marfc = marfc[(marfc["variable"] == "discharge") & (marfc["issue_time"] <= end)]
    m_issues = pd.DatetimeIndex(sorted(marfc["issue_time"].unique()))
    m_pairs = pd.concat([pairs_from_long(marfc, q, leads), *[pairs_from_cube(c, q) for c in all_cubes(m_issues)]], ignore_index=True)
    m_scores, _ = score_pairs(m_pairs, protocol, reference="persistence")
    _, vs_marfc = score_pairs(m_pairs[m_pairs["lead_h"] <= MARFC_MAX_LEAD_H], protocol, reference="marfc_rvf")
    vs_opponent = [vs_marfc.assign(opponent="marfc_rvf")]
    if retro is not None:
        _, vs_nwm = score_pairs(m_pairs[(m_pairs["lead_h"] > MARFC_MAX_LEAD_H) & (m_pairs["model"] != "marfc_rvf")], protocol, reference="nwm_retrospective")
        vs_opponent.append(vs_nwm.assign(opponent="nwm_retrospective"))
    info = {
        "window": f"{start:%Y-%m-%d} to {end:%Y-%m-%d}",
        "issues": len(issues),
        "marfc_issues": len(m_issues),
        "train_window": f"{protocol.train_window[0]:%Y-%m-%d} to {protocol.train_window[1]:%Y-%m-%d}",
        "upstream_gauges": list(inp.upstream),
        "routing_travel_h": models.routing.travel_h,
        "routing_window_h": models.routing.window_h,
        "gbm_iterations": {str(k): int(b.best_iteration) for k, b in models.gbm.boosters.items()},
        "fingerprint": protocol.fingerprint(),
    }
    return {
        "scores": scores,
        "vs_persistence": vs_persistence,
        "marfc_times_scores": m_scores,
        "vs_opponent": pd.concat(vs_opponent, ignore_index=True),
        "info": info,
    }


# ---------------------------------------------------------------- output

OPPONENT_LEADS = [6, 12, 24, 48, 72, 120, 168]
REPORT_LEADS = [1, 6, 12, 24, 48, 72, 120, 168]


def opponent_table(vs_opponent: pd.DataFrame, metric: str = "crps") -> pd.DataFrame:
    return _skill_table(vs_opponent, metric, OPPONENT_LEADS)


def render_md(result: dict) -> str:
    info = result["info"]
    notes = "\n".join(f"- `{m}`: {n}" for m, n in MODEL_NOTES.items())
    return "\n".join(
        [
            "# Strong baselines: validation years (WY2021-2022, the years with archived GEFS)",
            "",
            f"Fitted on {info['train_window']}; scored {info['window']} ({info['issues']} issues at 00/06/12/18Z; "
            f"{info['marfc_issues']} MARFC issues). Deterministic, so CRPS = MAE (ft3/s). Fingerprint `{info['fingerprint']}`.",
            "",
            notes,
            "",
            f"Routing travel times (h): {json.dumps(info['routing_travel_h'])}; attenuation windows (h): {json.dumps(info['routing_window_h'])}.",
            "",
            "## CRPS (ft3/s)",
            "",
            _md(table(result["scores"], "crps", REPORT_LEADS, fmt="{:.0f}")),
            "",
            "## KGE",
            "",
            _md(table(result["scores"], "kge", REPORT_LEADS, fmt="{:.3f}", ci=False)),
            "",
            "## CRPS skill vs persistence",
            "",
            _md(_skill_table(result["vs_persistence"], "crps", REPORT_LEADS)),
            "",
            "## Skill vs best opponent (at MARFC issue times): MARFC through 72 h, NWM retrospective beyond",
            "",
            "Positive = better than the opponent. The NWM retrospective is a perfect-forcing simulation without data "
            "assimilation, the only NWM run covering these years.",
            "",
            _md(opponent_table(result["vs_opponent"])),
            "",
            "## CRPS at MARFC issue times (ft3/s)",
            "",
            _md(table(result["marfc_times_scores"], "crps", OPPONENT_LEADS, fmt="{:.0f}")),
            "",
        ]
    )


def write_results(result: dict, out_dir: str | Path, lake: Lake | None = None, site_id: str = "USGS-01427510") -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for key in ("scores", "vs_persistence", "marfc_times_scores", "vs_opponent"):
        result[key].to_csv(out / f"{key}.csv", index=False)
    (out / "info.json").write_text(json.dumps(result["info"], indent=2, default=str))
    (out / "scoreboard.md").write_text(render_md(result))
    if lake is not None:
        payload = {k: json.loads(result[k].to_json(orient="records")) for k in ("scores", "vs_persistence", "marfc_times_scores", "vs_opponent")}
        payload["info"] = result["info"]
        payload["notes"] = MODEL_NOTES
        lake.write(f"metrics/{site_id}/strong-baselines/payload.json", json.dumps(payload, default=str).encode(), "application/json")
