"""Fit the flow stretch (`calibrate.FlowTailCalibration`) on hindcast directories, and write calibrated hindcasts.

`fit_flow_calibration` writes a calibration directory:

* `basin_stats.parquet`: per basin, from the cube target over the training years (WY2001-2019): hourly-flow
  quantiles at `LEVELS` (for the forecast-percentile feature), `delta` (1% of the mean flow) and `rb`, the
  Richards-Baker flashiness of daily means;
* `flow_calibration.csv`: stretch parameters per `wy`, lead and cell. The rows of a validation water year are fitted
  on the *other* validation years; `wy = 0` is fitted on all of them and is what any other year (operations, the
  frozen test years) uses;
* `flow_calibration.json`: the model the fit is for, and the flashiness and percentile edges.

`calibrate.apply_long` adds a calibrated copy (`<model>_cal`) of that model's forecasts to a long-format frame; every issue
uses the fit that held out its own water year, so scoring the copy on the validation years is never in-sample.
The extra upper-tail boost (`FlowTailCalibration.fit_boost`) is not fitted here and is off: it only applies when a
`flow_boost.csv` (lead_h, from_pct, kappa) is placed in the calibration directory.

Fitting thins low-flow rows (all rows with the forecast median above the 95th percentile are kept, fewer below;
`KEEP`) and reweights them by the inverse keep rate, so the fit sees enough rare high-flow rows without holding
every row of every basin in memory. Rows are weighted by 1 / (the basin's persistence CRPS at that lead in the
fitting years), as the cross-basin skill score implies. Nothing at or after WY2023 is read.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import tempfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_eval.baselines.persistence import last_available
from flowcast_eval.protocol import VALIDATION, HindcastProtocol

from .calibrate import ALL_YEARS, CAL_SUFFIX, LEVELS, PCT_EDGES, FlowCalibration, FlowTailCalibration, apply_long, basin_quantiles, climatology_percentile, load_calibration, water_year
from .cube import FROZEN_TEST_START, Cube, CubeDims
from .score import cube_obs_cfs, site_files

log = logging.getLogger(__name__)

# Share of rows kept for fitting, per forecast-percentile bin (PCT_EDGES: <50, 50-80, 80-95, 95-99, >=99).
KEEP = (0.1, 0.15, 0.3, 1.0, 1.0)
# A forecast-state cell needs this many rows of its own fit, else it falls back to its percentile bin or the lead.
MIN_CELL_ROWS = 2_000
DELTA_SHARE = 0.01
MIN_TRAIN_HOURS = 8760


# ---------------------------------------------------------------------------------------------- basin statistics


def richards_baker(hourly: pd.Series) -> float:
    """Richards-Baker flashiness of daily means (days with at least 20 hours), over consecutive-day pairs."""
    h = hourly.dropna()
    daily = h.resample("D").mean().where(h.resample("D").count() >= 20).dropna()
    both = daily.index.to_series().diff() == pd.Timedelta(days=1)
    if both.sum() <= 365:
        return np.nan
    return float(daily.diff().abs()[both].sum() / daily[both].sum())


def basin_stats(obs: pd.Series, protocol: HindcastProtocol = VALIDATION) -> dict | None:
    lo, hi = protocol.train_window
    train = obs[lo:hi].dropna()
    if len(train) < MIN_TRAIN_HOURS:
        return None
    q = np.quantile(train.to_numpy(np.float64), LEVELS)
    return {"delta": DELTA_SHARE * float(train.mean()), "rb": richards_baker(train), **{f"q{lv:.3f}": v for lv, v in zip(LEVELS, q)}}


# ---------------------------------------------------------------------------------------------- forecasts


def _member_array(fc: pd.DataFrame) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """(issues, leads, values [issue, lead, member]) of one model's long-format forecasts."""
    ci, issues = pd.factorize(fc["issue_time"], sort=True)
    cl, leads = pd.factorize(fc["lead_h"], sort=True)
    cm, _ = pd.factorize(fc["member"], sort=True)
    x = np.full((len(issues), len(leads), cm.max() + 1), np.nan, np.float32)
    x[ci, cl, cm] = fc["value"].to_numpy(np.float32)
    return pd.DatetimeIndex(issues), np.asarray(leads, float), x


def _on_cycle(fc: pd.DataFrame, protocol: HindcastProtocol, end: pd.Timestamp) -> pd.DataFrame:
    keep = fc["issue_time"].dt.hour.isin(protocol.issue_hours_utc) & (fc["issue_time"].dt.minute == 0) & (fc["valid_time"] <= pd.Timestamp(end, tz="UTC"))
    return fc[keep]


def _site_rows(job) -> tuple[str, dict | None, dict]:
    """One site's basin statistics and thinned fitting rows per lead."""
    sid, paths, model, cube_paths, dims, target, unit, area, protocol, end = job
    basin = sid.removeprefix("USGS-")
    obs = cube_obs_cfs(Cube(cube_paths, CubeDims.from_dict(dims)), basin, target, unit, area, end)
    stats = basin_stats(obs, protocol)
    if stats is None:
        return basin, None, {}
    cols = ["model", "issue_time", "valid_time", "lead_h", "member", "value"]
    fc = pd.concat([pd.read_parquet(p, columns=cols) for p in paths], ignore_index=True)
    fc = _on_cycle(fc[fc["model"] == model], protocol, end)
    if fc.empty:
        return basin, None, {}
    issues, leads, x = _member_array(fc)
    pers, _ = last_available(obs, issues, 1.0, 6.0)
    wy = water_year(issues)
    rng = np.random.default_rng(zlib.crc32(basin.encode()))
    q = basin_quantiles(pd.Series(stats))
    rows = {}
    for j, lead in enumerate(leads):
        y = obs.reindex(issues + pd.Timedelta(hours=float(lead))).to_numpy(float)
        ok = np.isfinite(y) & np.isfinite(pers) & np.isfinite(x[:, j]).all(axis=1)
        if not ok.any():
            continue
        xs = np.sort(x[ok, j], axis=1)
        n = xs.shape[1]
        pct = climatology_percentile(0.5 * (xs[:, (n - 1) // 2] + xs[:, n // 2]), LEVELS, q)
        p_keep = np.asarray(KEEP)[np.digitize(pct, PCT_EDGES)]
        keep = rng.random(len(pct)) < p_keep
        perr = pd.DataFrame({"wy": wy[ok], "e": np.abs(pers[ok] - y[ok])}).groupby("wy")["e"].agg(["sum", "count"])
        rows[float(lead)] = {"x": xs[keep], "y": y[ok][keep], "pct": pct[keep], "wy": wy[ok][keep], "inv_p": 1.0 / p_keep[keep], "pers": perr}
    log.info("rows for %s", sid)
    return basin, stats, rows


def _fit_job(job) -> pd.DataFrame:
    path, lead, wy, flash_edges, seed = job
    d = np.load(path)
    years = np.unique(d["wy"])
    fit_years = years if wy == ALL_YEARS else years[years != wy]
    cols = np.isin(d["pers_years"], fit_years)
    pers_mean = d["pers_sum"][:, cols].sum(axis=1) / np.maximum(d["pers_n"][:, cols].sum(axis=1), 1)
    sel = np.isin(d["wy"], fit_years) & (pers_mean[d["site"]] > 0)
    site = d["site"][sel]
    weight = d["inv_p"][sel] / pers_mean[site]
    cal = FlowTailCalibration.fit(lead, d["x"][sel], d["y"][sel], d["delta"][site], weight, d["pct"][sel], d["rb"][site], flash_edges, min_rows=MIN_CELL_ROWS, seed=seed)
    log.info("fitted lead %g h, wy %d: %d cells", lead, wy, len(cal.params))
    return cal.params.assign(wy=wy)


def fit_flow_calibration(
    forecast_dirs: list[str | Path],
    cube_paths: list[str],
    out: str | Path,
    model: str | None = None,
    target: str = "qobs_mm_h",
    unit: str = "mm/h",
    area_attribute: str | None = "area_km2",
    dims: dict | None = None,
    protocol: HindcastProtocol = VALIDATION,
    workers: int = 1,
    seed: int = 0,
) -> FlowCalibration:
    """Fit the flow stretch on the validation-year hindcasts of `model` and write the calibration directory."""
    end = min(protocol.test_window[1].tz_localize(None), FROZEN_TEST_START - pd.Timedelta(hours=1))
    cube = Cube(cube_paths, CubeDims.from_dict(dims))
    files = site_files([Path(p) for p in forecast_dirs])
    if model is None:
        names = {p.stem for paths in files.values() for p in paths}
        if len(names) != 1:
            raise ValueError(f"several models in the forecast directories ({sorted(names)}); pass model=")
        model = names.pop()
    files = {sid: [p for p in paths if p.stem == model] for sid, paths in files.items()}
    files = {sid: paths for sid, paths in files.items() if paths and sid.removeprefix("USGS-") in set(cube.basins)}
    static = cube.load_static([s.removeprefix("USGS-") for s in files], [area_attribute]) if area_attribute and cube.has(area_attribute) else pd.DataFrame()
    jobs = [(sid, [str(p) for p in paths], model, [str(c) for c in cube_paths], dims, target, unit,
             float(static.loc[sid.removeprefix("USGS-"), area_attribute]) if area_attribute in static else None, protocol, end) for sid, paths in files.items()]
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max(workers, 1), mp_context=ctx) as pool:
        results = [r for r in pool.map(_site_rows, jobs) if r[1] is not None]
    stats = pd.DataFrame({b: s for b, s, _ in results}).T.rename_axis("basin")
    stats["rb"] = stats["rb"].fillna(stats["rb"].median())
    site_pos = {b: i for i, b in enumerate(stats.index)}
    flash_edges = tuple(float(e) for e in np.quantile(stats["rb"], [1 / 3, 2 / 3]))
    leads = sorted({lead for _, _, rows in results for lead in rows})
    years = sorted({int(y) for _, _, rows in results for r in rows.values() for y in r["pers"].index})
    if len(years) < 2:
        log.warning("only water year(s) %s: no held-out fits, the all-years fit is in-sample on them", years)
    with tempfile.TemporaryDirectory(dir=out if Path(out).exists() else None) as work:
        fit_jobs = []
        for lead in leads:
            parts = [(site_pos[b], rows[lead]) for b, _, rows in results if lead in rows]
            pers_sum = np.zeros((len(stats), len(years)))
            pers_n = np.zeros((len(stats), len(years)))
            for i, r in parts:
                for k, y in enumerate(years):
                    if y in r["pers"].index:
                        pers_sum[i, k], pers_n[i, k] = r["pers"].loc[y, "sum"], r["pers"].loc[y, "count"]
            path = Path(work) / f"lead_{lead:g}.npz"
            np.savez(path, x=np.concatenate([r["x"] for _, r in parts]), y=np.concatenate([r["y"] for _, r in parts]),
                     pct=np.concatenate([r["pct"] for _, r in parts]), wy=np.concatenate([r["wy"] for _, r in parts]),
                     inv_p=np.concatenate([r["inv_p"] for _, r in parts]), site=np.concatenate([np.full(len(r["y"]), i) for i, r in parts]),
                     delta=stats["delta"].to_numpy(float), rb=stats["rb"].to_numpy(float), pers_sum=pers_sum, pers_n=pers_n, pers_years=np.asarray(years))
            fit_jobs += [(str(path), lead, wy, flash_edges, seed) for wy in [*(years if len(years) > 1 else []), ALL_YEARS]]
        del results
        with ProcessPoolExecutor(max(workers, 1), mp_context=ctx) as pool:
            params = pd.concat(list(pool.map(_fit_job, fit_jobs)), ignore_index=True)
    folds = {int(wy): FlowTailCalibration(flash_edges, p.drop(columns="wy").reset_index(drop=True)) for wy, p in params.groupby("wy")}
    cal = FlowCalibration(model, folds, stats)
    cal.save(out)
    return cal


# ---------------------------------------------------------------------------------------------- apply


def _apply_site(job) -> None:
    sid, paths, cal_path, out = job
    cal = load_calibration(cal_path)
    fc = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    fc = apply_long(fc[fc["model"] == cal.model], cal, sid)
    fc = fc[fc["model"] == cal.model + CAL_SUFFIX]
    if fc.empty:
        return
    target = Path(out) / f"site_id={sid}" / f"{cal.model}{CAL_SUFFIX}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    fc.to_parquet(target, index=False)


def apply_flow_calibration(forecast_dirs: list[str | Path], calibration: str | Path, out: str | Path, workers: int = 1) -> None:
    """Write calibrated hindcasts (`<model>_cal`, long format, one file per site) of the calibrated model."""
    cal = load_calibration(str(calibration))
    files = site_files([Path(p) for p in forecast_dirs])
    jobs = [(sid, [str(p) for p in paths if p.stem == cal.model], str(calibration), str(out)) for sid, paths in files.items()]
    jobs = [j for j in jobs if j[1]]
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "_hindcast.json").write_text(json.dumps({"model": cal.model + CAL_SUFFIX, "calibration": str(calibration)}))
    with ProcessPoolExecutor(max(workers, 1), mp_context=multiprocessing.get_context("spawn")) as pool:
        list(pool.map(_apply_site, jobs))
