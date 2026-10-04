"""Score water-temperature hindcasts on the validation years (WY2021-2022) with the evaluation harness.

Per basin, against the observed hourly water temperature in the training cube (`tw_c`, hour-ending means):

* **hourly** (issues at 00/06/12/18Z, leads 1-168 h): flowcast runs vs persistence (last observation, 1 h latency)
  and diurnal persistence (the same hour of the last observed day);
* **daily maximum** (issues at 12Z, days 0-7; a day's maximum is the largest hour-ending mean of its local date,
  from days with at least 20 observed hours): flowcast runs vs yesterday's maximum (persistence), day-of-year
  climatology, and air2stream fitted on the training years with basin AORC daily-maximum air temperature, driven by
  observed AORC air temperature (perfect forcing) or by GEFS forecast air temperature (operational: the latest 00Z
  run available at issue time, members 0-4 averaged, bias-corrected per lead day on the GEFSv12 reforecast of the
  training years);
* **thresholds**: Brier score and hit/false-alarm counts of the daily maximum exceeding trout-stress thresholds.

Flowcast forecast directories are pooled into ensembles by label (e.g. three seeds -> one ensemble), and every
comparison is on the issue times all models share (plan §8.3). Nothing at or after WY2023 is read.

Calibrated copies (`<model>_cal`): per lead, `median + offset + scale * (member - median)`, each validation water year
fitted on the other (plus an all-years fit, `wy = 0`, for any other year).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_eval.baselines import Air2Stream, Climatology, climatology, daily_persistence, persistence
from flowcast_eval.baselines.air2stream import _rate
from flowcast_eval.metrics import crps_ensemble
from flowcast_eval.pairs import ForecastCube, pairs_from_cube, pairs_from_long
from flowcast_eval.protocol import DAILY_LEADS_D, HOURLY_LEADS_H, VALIDATION, HindcastProtocol
from flowcast_eval.schema import DAILY_VARIABLES, normalize_forecasts
from flowcast_eval.scoring import score_pairs

from .calibrate import ALL_YEARS
from .cube import FROZEN_TEST_START, Cube
from .hindcast import local_days

log = logging.getLogger(__name__)

TIMEZONE = "America/New_York"
HOURLY = VALIDATION.with_window("2020-10-01", "2022-09-30T23:00", name="temperature-hourly-wy2021-2022")
DAILY = replace(HOURLY, name="temperature-daily-max-wy2021-2022", issue_hours_utc=(12,), leads_h=tuple(24.0 * d for d in DAILY_LEADS_D))
# Trout thresholds for the daily maximum: 21 degC (70 degF) stress for brown and rainbow trout, 23.9 degC (75 degF)
# the upper Delaware thermal-release target (and the USGS forecast's exceedance product).
THRESHOLDS_C = (21.0, 23.9)
MIN_HOURS_PER_DAY = 20
# Baselines fitted on the training years (climatology, air2stream) need at least this many daily maxima there.
MIN_TRAIN_DAYS = 60


# ---------------------------------------------------------------------------------------------- observations


def daily_max(hourly: pd.Series, min_hours: int = MIN_HOURS_PER_DAY, timezone: str = TIMEZONE) -> pd.Series:
    """Local-date maximum of hour-ending values, indexed by midnight UTC of the local date (harness convention)."""
    s = hourly.dropna()
    dates, _ = local_days(pd.DatetimeIndex(s.index).tz_convert(None) if s.index.tz is not None else pd.DatetimeIndex(s.index), timezone)
    g = s.groupby(dates)
    out = g.max()[g.count() >= min_hours]
    return out.set_axis(pd.DatetimeIndex(out.index).tz_localize("UTC"))


def daily_mean(hourly: pd.Series, timezone: str = TIMEZONE) -> pd.Series:
    s = hourly.dropna()
    dates, _ = local_days(pd.DatetimeIndex(s.index).tz_convert(None) if s.index.tz is not None else pd.DatetimeIndex(s.index), timezone)
    out = s.groupby(dates).mean()
    return out.set_axis(pd.DatetimeIndex(out.index).tz_localize("UTC"))


def _naive_days(s: pd.Series) -> pd.Series:
    return s.set_axis(pd.DatetimeIndex(s.index).tz_convert(None))


# ---------------------------------------------------------------------------------------------- baselines


def diurnal_persistence(obs: pd.Series, issue_times: pd.DatetimeIndex, leads_h, site_id: str, latency_h: float = 1.0) -> ForecastCube:
    """The same hour of the latest observed day: value at t + l is the observation at t + l - 24k (k >= 1 minimal)."""
    leads = np.asarray(leads_h, float)
    s = obs[~obs.index.duplicated()].sort_index()
    k = np.ceil((leads + latency_h) / 24.0)
    times = issue_times.values[:, None] + ((leads - 24.0 * k) * 3600 * 1e9).astype("timedelta64[ns]")[None, :]
    values = s.reindex(pd.DatetimeIndex(times.ravel()).tz_localize("UTC")).to_numpy(float).reshape(len(issue_times), len(leads))
    return ForecastCube("persistence_diurnal", site_id, "water_temperature", issue_times, leads, values[:, :, None])


def gefs_daily_max_air(cube: Cube, basin: str, issue_times: pd.DatetimeIndex, n_days: int, product: str = "gefs_temp_2m_c", latency_h: float = 6.0, members: int = 5, timezone: str = TIMEZONE) -> np.ndarray:
    """Daily-maximum GEFS air temperature [issue, day] (member mean) from the latest init available at issue time."""
    start = issue_times.min().tz_convert(None) - pd.Timedelta(days=2)
    end = issue_times.max().tz_convert(None)
    inits, leads, values, _ = next(iter(cube.load_forecast(basin, [product], start, end).values()))
    mean = np.nanmean(values[:, :, :members, 0], axis=2)
    out = np.full((len(issue_times), n_days), np.nan)
    naive = issue_times.tz_convert(None)
    pos = inits.searchsorted(naive - pd.Timedelta(hours=latency_h), side="right") - 1
    issue_date, _ = local_days(naive + pd.Timedelta(hours=1), timezone)
    for i, p in enumerate(pos):
        if p < 0:
            continue
        valid = inits[p] + pd.to_timedelta(leads, unit="h")
        keep = valid > naive[i]
        dates, _ = local_days(valid[keep], timezone)
        day = (dates - issue_date[i]).astype(int)
        v = mean[p][keep]
        for k in range(n_days):
            sel = (day == k) & np.isfinite(v)
            if sel.sum() >= 3:
                out[i, k] = v[sel].max()
    return out


def gefs_bias_by_day(cube: Cube, basin: str, aorc_tmax: pd.Series, train_end: pd.Timestamp, n_days: int) -> np.ndarray:
    """Mean AORC-minus-GEFSv12-reforecast daily-maximum air temperature per lead day, training years only."""
    rf = cube.load_forecast(basin, ["gefs_rf_temp_2m_c"], pd.Timestamp("2000-10-01"), train_end)
    inits = next(iter(rf.values()))[0]
    issues = pd.DatetimeIndex(inits + pd.Timedelta(hours=12)).tz_localize("UTC")
    fc = gefs_daily_max_air(cube, basin, issues, n_days, product="gefs_rf_temp_2m_c")
    obs = _naive_days(aorc_tmax)
    issue_days = issues.tz_convert(None).floor("D")
    target = np.stack([obs.reindex(issue_days + pd.Timedelta(days=k)).to_numpy(float) for k in range(n_days)], axis=1)
    with np.errstate(all="ignore"):
        bias = np.nanmean(target - fc, axis=0)
    return np.nan_to_num(bias)


def air2stream_forecast(model: Air2Stream, tw_daily: pd.Series, ta: np.ndarray, q_daily: pd.Series, issue_times: pd.DatetimeIndex, site_id: str, name: str, run_type: str) -> ForecastCube:
    """air2stream from the last observed day before the issue date, air temperature per issue and day `ta[issue, day]`."""
    lead_days = np.arange(ta.shape[1])
    issue_days = issue_times.tz_convert(None).floor("D")
    tw = _naive_days(tw_daily).reindex(issue_days - pd.Timedelta(days=1)).to_numpy(float)
    q0 = _naive_days(q_daily).reindex(issue_days - pd.Timedelta(days=1)).to_numpy(float)
    theta = q0 / model.q_mean
    values = np.full(ta.shape, np.nan)
    for k in lead_days:
        doy = (issue_days + pd.Timedelta(days=int(k))).dayofyear.to_numpy()
        tw = np.clip(tw + _rate(model.params, tw, ta[:, k], theta, doy), 0.0, 40.0)
        values[:, k] = tw
    return ForecastCube(name, site_id, "water_temperature_daily_max", issue_times, 24.0 * lead_days, values[:, :, None], run_type=run_type)


# ---------------------------------------------------------------------------------------------- flowcast forecasts


def load_forecasts(groups: dict[str, list[Path]], site: str) -> pd.DataFrame:
    """One site's forecasts; runs in a group are pooled into one ensemble named `<label><mode suffix>`. Hourly
    forecasts are kept on the harness's lead grid, so hindcasts written with `all_leads` score the same."""
    frames = []
    for label, dirs in groups.items():
        for r, root in enumerate(dirs):
            meta = json.loads((Path(root) / "_hindcast.json").read_text()) if (Path(root) / "_hindcast.json").exists() else {}
            base = meta.get("model", "")
            for p in sorted((Path(root) / f"site_id={site}").glob("*.parquet")):
                df = pd.read_parquet(p, filters=[[("variable", "in", sorted(DAILY_VARIABLES))], [("lead_h", "in", [float(h) for h in HOURLY_LEADS_H])]])
                suffix = df["model"].iloc[0][len(base) :] if base and df["model"].iloc[0].startswith(base) else f"_{df['model'].iloc[0]}"
                df["model"] = f"{label}{suffix}"
                df["member"] = df["member"] + 1000 * r
                frames.append(df.assign(site_id=site))
    if not frames:
        return pd.DataFrame()
    return normalize_forecasts(pd.concat(frames, ignore_index=True))


def exceedance(values: np.ndarray, obs: np.ndarray, thr: float) -> dict[str, float]:
    """values [n, members] (NaN-padded); Brier score of P(> thr) plus contingency counts of the median forecast."""
    ok = np.isfinite(obs) & np.isfinite(values).any(axis=1)
    v, o = values[ok], obs[ok] > thr
    if not len(o):
        return {"n": 0}
    # members are NaN-padded when models with different ensemble sizes share a table
    prob = (v > thr).sum(axis=1) / np.isfinite(v).sum(axis=1)
    point = np.nanmedian(v, axis=1) > thr
    return {"n": int(len(o)), "observed": int(o.sum()), "brier_sum": float(((prob - o) ** 2).sum()), "hits": int((point & o).sum()), "misses": int((~point & o).sum()), "false_alarms": int((point & ~o).sum())}


def member_table(long: pd.DataFrame) -> pd.DataFrame:
    """Forecast values indexed by (model, issue_time, lead_h, valid_time), one column per member (or quantile)."""
    long = long.copy()
    col = "member" if long.get("member") is not None and long["member"].notna().any() else None
    if col is None and "quantile" in long and long["quantile"].notna().any():
        col = "quantile"
    long["_m"] = long[col] if col else 0
    return long.pivot_table(index=["model", "issue_time", "lead_h", "valid_time"], columns="_m", values="value")


def threshold_table(tables: list[pd.DataFrame], obs: pd.Series, leads_h) -> pd.DataFrame:
    """Exceedance scores per model, lead and threshold on the (issue, lead) pairs every model forecasts."""
    ok = [t[np.isfinite(obs.reindex(pd.DatetimeIndex(t.index.get_level_values("valid_time"))).to_numpy(float))] for t in tables if len(t)]
    keys = None
    for t in ok:
        for model, g in t.groupby(level="model"):
            k = set(zip(g.index.get_level_values("issue_time"), g.index.get_level_values("lead_h")))
            keys = k if keys is None else keys & k
    rows = []
    for t in ok:
        for model, g in t.groupby(level="model"):
            for lead in leads_h:
                sel = g[(g.index.get_level_values("lead_h") == lead) & [(i, l) in keys for i, l in zip(g.index.get_level_values("issue_time"), g.index.get_level_values("lead_h"))]]
                o = obs.reindex(pd.DatetimeIndex(sel.index.get_level_values("valid_time"))).to_numpy(float)
                for thr in THRESHOLDS_C:
                    rows.append({"model": model, "lead_h": lead, "threshold_c": thr, **exceedance(sel.to_numpy(float), o, thr)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------- calibration

SCALE_GRID = np.round(np.arange(0.6, 2.61, 0.1), 2)
ABLATION_SUFFIXES = ("_noflow", "_obsflow")
CAL_KEYS = ["model", "variable", "lead_h", "wy"]


def water_year(t: pd.Series) -> pd.Series:
    return t.dt.year + (t.dt.month >= 10).astype(int)


def _members(fc: pd.DataFrame, obs: pd.Series) -> pd.DataFrame:
    """Wide member table with the verifying observation (`_obs`), indexed by `CAL_KEYS`."""
    wide = fc.pivot_table(index=["model", "variable", "issue_time", "lead_h", "valid_time"], columns="member", values="value")
    o = obs.reindex(pd.DatetimeIndex(wide.index.get_level_values("valid_time"))).to_numpy(float)
    wide = wide[np.isfinite(o)]
    keys = wide.index.to_frame(index=False)
    wide.insert(0, "_obs", o[np.isfinite(o)])
    wide.index = pd.MultiIndex.from_arrays([keys["model"], keys["variable"], keys["lead_h"], water_year(keys["issue_time"]).to_numpy()], names=CAL_KEYS)
    return wide


def _calibration_data(site: str, groups, cube_paths) -> list[pd.DataFrame]:
    basin = site.removeprefix("USGS-")
    df = Cube(cube_paths).load_dynamic(basin, ["tw_c"], pd.Timestamp("2020-09-01"), FROZEN_TEST_START - pd.Timedelta(hours=1))
    df.index = df.index.tz_localize("UTC")
    fc = load_forecasts(groups, site)
    if fc.empty or df["tw_c"].dropna().empty:
        return []
    fc = fc[(fc["issue_time"].dt.minute == 0) & ~fc["model"].str.endswith(ABLATION_SUFFIXES)]
    hourly = fc[(fc["variable"] == "water_temperature") & fc["issue_time"].dt.hour.isin(HOURLY.issue_hours_utc)]
    daily = fc[(fc["variable"] == "water_temperature_daily_max") & fc["issue_time"].dt.hour.isin(DAILY.issue_hours_utc)]
    return [_members(part, obs) for part, obs in ((hourly, df["tw_c"]), (daily, daily_max(df["tw_c"]))) if not part.empty]


def site_sums(table: pd.DataFrame, offsets: dict | None = None) -> list[dict]:
    """Per `CAL_KEYS` group of a `_members` table: count and sum of obs - median; with `offsets` (per key), CRPS sums
    over a grid of spread factors after adding that offset."""
    rows = []
    for key, g in table.groupby(level=CAL_KEYS):
        o, v = g["_obs"].to_numpy(), g.drop(columns="_obs").to_numpy(float)
        med = np.nanmedian(v, axis=1)
        row = dict(zip(CAL_KEYS, key), n=len(o), resid_sum=float(np.sum(o - med)))
        if offsets is not None:
            off = offsets.get(key, 0.0)
            dev = v - med[:, None]
            for sc in SCALE_GRID:
                row[f"crps_{sc}"] = float(np.nansum(crps_ensemble(med[:, None] + off + sc * dev, o)))
        rows.append(row)
    return rows


def calibration_sums(job) -> pd.DataFrame | None:
    """`site_sums` of one site's hourly and daily-maximum forecasts (pass 1 without `offsets`, pass 2 with)."""
    site, groups, cube_paths, offsets = job
    rows = [r for table in _calibration_data(site, groups, cube_paths) for r in site_sums(table, offsets)]
    return pd.DataFrame(rows) if rows else None


def fit_temperature_calibration(sites: list[str], groups: dict[str, list[Path]], cube_paths: list[str], workers: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(summed statistics, `fit_calibration`) over `sites`: pass 1 sums the residuals for each year's offset, pass 2
    the CRPS over spread factors around those offsets."""
    with ProcessPoolExecutor(max(workers, 1), mp_context=multiprocessing.get_context("spawn")) as pool:
        first = pd.concat([r for r in pool.map(calibration_sums, [(s, groups, cube_paths, None) for s in sites]) if r is not None], ignore_index=True)
        first = first.groupby(CAL_KEYS, as_index=False)[["n", "resid_sum"]].sum()
        offsets = {tuple(r[k] for k in CAL_KEYS): r["resid_sum"] / r["n"] for _, r in first.iterrows()}
        sums = pd.concat([r for r in pool.map(calibration_sums, [(s, groups, cube_paths, offsets) for s in sites]) if r is not None], ignore_index=True)
    sums = sums.groupby(CAL_KEYS, as_index=False).sum()
    return sums, fit_calibration(sums)


def fit_calibration(sums: pd.DataFrame) -> pd.DataFrame:
    """Offset and spread factor per (model, variable, lead): for each validation water year fitted on the *other*
    years, and for `wy = ALL_YEARS` on all of them. In the pass-2 CRPS sums every year carries its own offset, so the
    spread factor is chosen around offsets fitted like the applied ones."""
    out = []
    for (model, variable, lead), g in sums.groupby(["model", "variable", "lead_h"]):
        for wy in [*sorted(g["wy"].unique()), ALL_YEARS]:
            fit = g[g["wy"] != wy]
            if fit.empty or fit["n"].sum() == 0:
                continue
            crps = {sc: fit[f"crps_{sc}"].sum() for sc in SCALE_GRID}
            out.append({"model": model, "variable": variable, "lead_h": lead, "wy": wy, "offset": fit["resid_sum"].sum() / fit["n"].sum(), "scale": min(crps, key=crps.get), "fit_n": int(fit["n"].sum())})
    return pd.DataFrame(out)


def apply_calibration(fc: pd.DataFrame, cal: pd.DataFrame | None) -> pd.DataFrame:
    """Calibrated copies (`<model>_cal`) of flowcast forecasts: median + offset + scale x (member - median). Issues in
    a held-out validation year use that year's fit, others the all-years fit."""
    if cal is None or cal.empty or fc.empty:
        return fc
    held_out = set(cal["wy"]) - {ALL_YEARS}
    wy = water_year(fc["issue_time"])
    f = fc.assign(wy=wy.where(wy.isin(held_out), ALL_YEARS))
    f = f.merge(cal[[*CAL_KEYS, "offset", "scale"]], on=CAL_KEYS, how="inner")
    if f.empty:
        return fc
    med = f.groupby(["model", "variable", "issue_time", "lead_h"])["value"].transform("median")
    f["value"] = med + f["offset"] + f["scale"] * (f["value"] - med)
    f["model"] = f["model"] + "_cal"
    return pd.concat([fc, f[fc.columns]], ignore_index=True)


# ---------------------------------------------------------------------------------------------- per site


def score_site(job) -> dict[str, pd.DataFrame] | None:
    site, groups, cube_paths, n_boot, fit_dir, cal = job
    basin = site.removeprefix("USGS-")
    cube = Cube(cube_paths)
    end = FROZEN_TEST_START - pd.Timedelta(hours=1)
    df = cube.load_dynamic(basin, ["tw_c", "aorc_temp_2m_c", "qobs_mm_h"], pd.Timestamp("2000-10-01"), end)
    df.index = df.index.tz_localize("UTC")
    tw = df["tw_c"].dropna()
    if tw[HOURLY.test_window[0] :].empty:
        return None
    forecasts = load_forecasts(groups, site)
    if forecasts.empty:
        return None
    forecasts = apply_calibration(forecasts, cal)
    train_end = pd.Timestamp(HOURLY.train_end)
    out: dict[str, pd.DataFrame] = {}

    # hourly
    hourly = forecasts[forecasts["variable"] == "water_temperature"]
    hourly = hourly[hourly["issue_time"].dt.hour.isin(HOURLY.issue_hours_utc) & (hourly["issue_time"].dt.minute == 0)]
    issues = HOURLY.issue_times(until=tw.index.max())
    leads = [h for h in HOURLY_LEADS_H if h <= 168]
    proto = replace(HOURLY, n_boot=n_boot, leads_h=tuple(leads))
    pairs = pd.concat(
        [pairs_from_long(hourly, tw, proto.leads_h), pairs_from_cube(persistence(tw, issues, leads, site, variable="water_temperature"), tw), pairs_from_cube(diurnal_persistence(tw, issues, leads, site), tw)],
        ignore_index=True,
    )
    out["hourly_scores"], out["hourly_vs_persistence"] = score_pairs(pairs, proto, reference="persistence")

    # daily maximum
    tw_max = daily_max(df["tw_c"])
    air_max = daily_max(df["aorc_temp_2m_c"], min_hours=24)
    q_mean = daily_mean(df["qobs_mm_h"])
    fit_path = Path(fit_dir) / f"{basin}.json" if fit_dir else None
    tr = slice(None, pd.Timestamp(train_end, tz="UTC"))
    a2s = None
    if fit_path is not None and fit_path.exists():
        a2s = Air2Stream.from_dict(json.loads(fit_path.read_text()))
    elif tw_max[tr].size >= MIN_TRAIN_DAYS:
        a2s = Air2Stream.fit(_naive_days(tw_max[tr]), _naive_days(air_max[tr]), _naive_days(q_mean[tr]).fillna(q_mean.mean()))
        if fit_path is not None:
            fit_path.parent.mkdir(parents=True, exist_ok=True)
            fit_path.write_text(json.dumps(a2s.to_dict()))
    daily = forecasts[forecasts["variable"] == "water_temperature_daily_max"]
    d_issues = DAILY.issue_times(until=tw.index.max())
    n_days = len(DAILY_LEADS_D)
    cubes = [daily_persistence(tw_max, d_issues, DAILY_LEADS_D, site, "water_temperature_daily_max")]
    doy = pd.DatetimeIndex(tw_max[tr].index).dayofyear
    # day-of-year climatology needs training maxima all year round (many gauges record only in summer)
    if tw_max[tr].size >= MIN_TRAIN_DAYS and len(np.unique(np.minimum(doy, 365))) == 365:
        cubes.append(climatology(Climatology.fit(tw_max[tr]), d_issues, DAILY.leads_h, site, "water_temperature_daily_max"))
    if a2s is not None:
        ta_obs = np.stack([_naive_days(air_max).reindex(d_issues.tz_convert(None).floor("D") + pd.Timedelta(days=k)).to_numpy(float) for k in range(n_days)], axis=1)
        ta_gefs = gefs_daily_max_air(cube, basin, d_issues, n_days) + gefs_bias_by_day(cube, basin, air_max, train_end, n_days)[None, :]
        cubes.append(air2stream_forecast(a2s, tw_max, ta_gefs, q_mean, d_issues, site, "air2stream_gefs_air", "operational"))
        cubes.append(air2stream_forecast(a2s, tw_max, ta_obs, q_mean, d_issues, site, "air2stream_obs_air", "perfect_forcing"))
    dproto = replace(DAILY, n_boot=n_boot)
    dpairs = pd.concat([pairs_from_long(daily[daily["issue_time"].dt.hour.isin(DAILY.issue_hours_utc)], tw_max, dproto.leads_h), *[pairs_from_cube(c, tw_max) for c in cubes]], ignore_index=True)
    out["daily_scores"], out["daily_vs_persistence"] = score_pairs(dpairs, dproto, reference="persistence")
    if a2s is not None:
        out["daily_vs_air2stream_gefs"] = score_pairs(dpairs, dproto, reference="air2stream_gefs_air")[1]

    # thresholds, on the issue times every model has
    tables = [member_table(daily[daily["issue_time"].dt.hour.isin(DAILY.issue_hours_utc)])] + [member_table(c.to_long()) for c in cubes]
    out["thresholds"] = threshold_table(tables, tw_max, dproto.leads_h)
    out["info"] = pd.DataFrame([{"air2stream_train_rmse": a2s.rmse_train if a2s is not None else np.nan, "hourly_obs_val": int(tw[HOURLY.test_window[0] :].size), "daily_obs_val": int(tw_max[HOURLY.test_window[0] :].size)}])
    for frame in out.values():
        frame.insert(0, "site_id", site)
    log.info("scored %s", site)
    return out


def _score_site_cached(job) -> dict[str, pd.DataFrame] | None:
    """score_site with its result kept on disk (reruns skip finished sites); a failing site is logged and skipped."""
    site, cache = job[0], Path(job[4]).parent / "sites" / f"{job[0]}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    try:
        res = score_site(job)
    except Exception:
        log.exception("scoring failed for %s", site)
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(res, cache)
    return res


def score_temperature(groups: dict[str, list[str]], cube_paths: list[str], out: str | Path, n_boot: int = 500, workers: int = 1, sites: list[str] | None = None, calibrate: bool = True) -> dict[str, pd.DataFrame]:
    """Score all sites. With `calibrate`, flowcast models also get cross-validated calibrated copies (`_cal`): per
    lead, an offset and a spread factor fitted on the other validation water year, pooled over all scored sites. The
    fit is written to `calibration.csv` and reused on reruns."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    groups = {k: [Path(p) for p in v] for k, v in groups.items()}
    found = sorted({p.name.split("=", 1)[1] for dirs in groups.values() for d in dirs for p in d.glob("site_id=*")})
    if sites:
        found = [s for s in found if s.removeprefix("USGS-") in {x.removeprefix("USGS-") for x in sites}]
    ctx = multiprocessing.get_context("spawn")
    cal = None
    if calibrate and (out / "calibration.csv").exists():
        cal = pd.read_csv(out / "calibration.csv")
        if not {"offset", "scale"} <= set(cal.columns) or ALL_YEARS not in set(cal["wy"]):
            log.warning("%s is from an older calibration format; refitting", out / "calibration.csv")
            cal = None
    if calibrate and cal is None:
        sums, cal = fit_temperature_calibration(found, groups, cube_paths, workers)
        sums.to_csv(out / "calibration_sums.csv", index=False)
        cal.to_csv(out / "calibration.csv", index=False)
        for cached in (out / "sites").glob("*.pkl"):  # scored with another calibration
            cached.unlink()
    jobs = [(s, groups, cube_paths, n_boot, str(out / "air2stream"), cal) for s in found]
    if workers > 1:
        with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
            results = [r for r in pool.map(_score_site_cached, jobs) if r is not None]
    else:
        results = [r for r in (_score_site_cached(j) for j in jobs) if r is not None]
    keys = dict.fromkeys(k for r in results for k in r)
    tables = {k: pd.concat([r[k] for r in results if k in r and not r[k].empty], ignore_index=True) for k in keys}
    for k, t in tables.items():
        t.to_csv(out / f"{k}.csv", index=False)
    return tables


USGS_LEVELS = np.round(np.arange(0.01, 1.0, 0.01), 2)


def two_piece_normal(q3: pd.DataFrame) -> pd.DataFrame:
    """Median and 90% interval (0.05/0.5/0.95 rows) -> 99 quantiles of a two-piece normal through them.

    Three quantiles make the harness's pinball-integral CRPS far too low (the median carries weight 0.45, not 1),
    which would flatter the USGS forecast; a fitted distribution scores it like the ensembles.
    """
    wide = q3.pivot_table(index=["issue_time", "valid_time"], columns="quantile", values="value")
    med, lo, hi = wide[0.5].to_numpy(), wide[0.05].to_numpy(), wide[0.95].to_numpy()
    z = norm_ppf(USGS_LEVELS)
    s_lo, s_hi = np.maximum(med - lo, 0) / 1.6449, np.maximum(hi - med, 0) / 1.6449
    values = med[:, None] + z[None, :] * np.where(z < 0, s_lo[:, None], s_hi[:, None])
    out = pd.DataFrame(values, index=wide.index, columns=USGS_LEVELS).stack().rename("value").reset_index().rename(columns={"level_2": "quantile"})
    return out


def norm_ppf(p: np.ndarray) -> np.ndarray:
    """Standard normal quantiles (Acklam's rational approximation, |error| < 1.2e-9)."""
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    p = np.asarray(p, float)
    out = np.empty_like(p)
    lo, hi = p < 0.02425, p > 1 - 0.02425
    mid = ~(lo | hi)
    q = np.sqrt(-2 * np.log(p[lo]))
    out[lo] = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p[mid] - 0.5
    r = q * q
    out[mid] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = np.sqrt(-2 * np.log(1 - p[hi]))
    out[hi] = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    return out


def lordville_usgs(paired_csv: str | Path, archive_parquet: list[str | Path] | None = None) -> pd.DataFrame:
    """USGS Delaware forecasts at Lordville (01427207) in the interchange format: median and 90% interval quantiles.

    `paired_csv` is `paired_predicted_observed_temperatures.csv` from the 2021 data release (doi:10.5066/P96R34A7;
    model `DA`, the operational one, and the +0 cfs scenario); `archive_parquet` adds the archiver's
    `usgs_drb_temp` issues. Issue time is local midnight of the issue date; valid time is midnight UTC of the date.
    """
    d = pd.read_csv(paired_csv)
    d = d[(d["site_name"] == "DR @ Lordville") & (d["model_name"] == "DA") & (d["scenario"] == "+0cfs")]
    issue = pd.to_datetime(d["issue_time"]).dt.tz_localize(TIMEZONE).dt.tz_convert("UTC")
    valid = pd.to_datetime(d["time"]).dt.tz_localize("UTC")
    rows = [pd.DataFrame({"issue_time": issue.values, "valid_time": valid.values, "quantile": q, "value": d[col].to_numpy(float)}) for q, col in ((0.05, "max_temp_l90"), (0.5, "max_temp_c_predicted"), (0.95, "max_temp_u90"))]
    frames = [pd.concat(rows, ignore_index=True)]
    for path in archive_parquet or []:
        a = pd.read_parquet(path)
        a = a[(a["usgs_site"] == "01427207") & (a["variable"] == "water_temp_max_f") & (a["qualifier"] == "0cfs")]
        local = a["valid_time"].dt.tz_convert(TIMEZONE).dt.tz_localize(None).dt.normalize()
        frames.append(pd.DataFrame({"issue_time": a["issue_time"].values, "valid_time": local.dt.tz_localize("UTC").values, "quantile": a["quantile"].to_numpy(float), "value": (a["value"].to_numpy(float) - 32.0) * 5.0 / 9.0}))
    out = pd.concat(frames, ignore_index=True)
    # the archive stores levels as float32; 0.05 must match the data release's 0.05
    out["quantile"] = out["quantile"].astype(float).round(4)
    out = two_piece_normal(out)
    out["issue_time"] = pd.to_datetime(out["issue_time"], utc=True)
    out["valid_time"] = pd.to_datetime(out["valid_time"], utc=True)
    out = out[out["valid_time"] < pd.Timestamp(FROZEN_TEST_START, tz="UTC")]
    out = out.assign(site_id="USGS-01427207", variable="water_temperature_daily_max", model="usgs_drb_temperature", unit="degC", run_type="operational")
    out["lead_h"] = ((out["valid_time"] - out["issue_time"].dt.tz_convert(TIMEZONE).dt.tz_localize(None).dt.normalize().dt.tz_localize("UTC")).dt.total_seconds() / 3600.0).round()
    return normalize_forecasts(out)


def score_lordville(usgs: pd.DataFrame, groups: dict[str, list[str]], cube_paths: list[str], fit_dir: str | Path, n_boot: int = 1000, obs_override: pd.Series | None = None, cal: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Head to head with the USGS forecast at its own issue times (local midnight), days 0-7."""
    site, basin = "USGS-01427207", "01427207"
    cube = Cube(cube_paths)
    df = cube.load_dynamic(basin, ["tw_c", "aorc_temp_2m_c", "qobs_mm_h"], pd.Timestamp("2000-10-01"), FROZEN_TEST_START - pd.Timedelta(hours=1))
    df.index = df.index.tz_localize("UTC")
    tw_max = daily_max(df["tw_c"]) if obs_override is None else obs_override
    air_max, q_mean = daily_max(df["aorc_temp_2m_c"], min_hours=24), daily_mean(df["qobs_mm_h"])
    a2s = Air2Stream.from_dict(json.loads((Path(fit_dir) / f"{basin}.json").read_text()))
    issues = pd.DatetimeIndex(sorted(usgs["issue_time"].unique()))
    fc = load_forecasts({k: [Path(p) for p in v] for k, v in groups.items()}, site)
    fc = fc[(fc["variable"] == "water_temperature_daily_max") & fc["issue_time"].isin(issues)]
    fc = apply_calibration(fc, cal)
    # the USGS issue is local midnight; lead days count from its local date, which is the UTC date at 04/05Z
    n_days = len(DAILY_LEADS_D)
    ta_gefs = gefs_daily_max_air(cube, basin, issues, n_days) + gefs_bias_by_day(cube, basin, air_max, pd.Timestamp(HOURLY.train_end), n_days)[None, :]
    ta_obs = np.stack([_naive_days(air_max).reindex(issues.tz_convert(None).floor("D") + pd.Timedelta(days=k)).to_numpy(float) for k in range(n_days)], axis=1)
    cubes = [
        daily_persistence(tw_max, issues, DAILY_LEADS_D, site, "water_temperature_daily_max"),
        air2stream_forecast(a2s, tw_max, ta_gefs, q_mean, issues, site, "air2stream_gefs_air", "operational"),
        air2stream_forecast(a2s, tw_max, ta_obs, q_mean, issues, site, "air2stream_obs_air", "perfect_forcing"),
    ]
    proto = HindcastProtocol(name="lordville-usgs", test_start=f"{issues.min():%Y-%m-%dT%H:%M}", test_end=f"{issues.max():%Y-%m-%dT%H:%M}", issue_hours_utc=tuple(sorted(set(issues.hour))), leads_h=DAILY.leads_h, n_boot=n_boot)
    pairs = pd.concat([pairs_from_long(fc, tw_max, proto.leads_h), pairs_from_long(usgs, tw_max, proto.leads_h), *[pairs_from_cube(c, tw_max) for c in cubes]], ignore_index=True)
    scores, vs_usgs = score_pairs(pairs, proto, reference="usgs_drb_temperature")
    return {"scores": scores, "vs_usgs": vs_usgs, "vs_persistence": score_pairs(pairs, proto, reference="persistence")[1], "issues": pd.DataFrame({"issue_time": issues})}
