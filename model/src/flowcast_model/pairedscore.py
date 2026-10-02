"""Paired streamflow comparison of runs against a control on the cells every run forecasts (validation years).

For each basin: fair ensemble CRPS (ft3/s) of each run's members at 00/06/12/18Z issues and the given leads,
persistence (the observation 1 h before issue), and skill = 1 - CRPS / CRPS(persistence). Every run and persistence
are scored on the same (issue, lead) cells. Differences against the control come with a paired moving-block
bootstrap over issue days (7-day blocks). Subsets are issue months, e.g. the summer low-flow season.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_eval.metrics import crps_ensemble, crps_quantiles
from flowcast_eval.scoring import block_bootstrap_counts

from .cube import FROZEN_TEST_START, Cube
from .hindcast import site_id
from .tempscore import apply_calibration, daily_max, fit_temperature_calibration, load_forecasts, lordville_usgs
from .units import M3_S_TO_CFS

log = logging.getLogger(__name__)

LEADS_H = (1, 6, 12, 24, 48, 72, 120, 168)
ISSUE_HOURS = (0, 6, 12, 18)
SUBSETS = {"all": tuple(range(1, 13)), "jun-sep": (6, 7, 8, 9), "oct-may": (10, 11, 12, 1, 2, 3, 4, 5)}


def observed_cfs(cube: Cube, basin: str, start: str = "2020-09-01") -> pd.Series:
    df = cube.load_dynamic(basin, ["qobs_m3s"], pd.Timestamp(start), FROZEN_TEST_START - pd.Timedelta(hours=1))
    return (df["qobs_m3s"] * M3_S_TO_CFS).tz_localize("UTC")


def run_crps(path: Path, obs: pd.Series, leads=LEADS_H) -> pd.Series | None:
    """CRPS per (issue_time, lead_h) of one run's hindcast file, at cycle issues only."""
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["issue_time", "lead_h", "member", "value", "valid_time"])
    df = df[(df["issue_time"].dt.minute == 0) & df["issue_time"].dt.hour.isin(ISSUE_HOURS) & df["lead_h"].isin(leads)]
    wide = df.pivot_table(index=["issue_time", "lead_h", "valid_time"], columns="member", values="value")
    o = obs.reindex(wide.index.get_level_values("valid_time")).to_numpy(float)
    crps = crps_ensemble(wide.to_numpy(float), o)
    return pd.Series(crps, index=wide.index.droplevel("valid_time"))


def basin_cells(basin: str, runs: dict[str, tuple[Path, str]], cube: Cube, leads=LEADS_H) -> pd.DataFrame | None:
    """One row per (issue, lead) that every run and persistence can score: CRPS columns per run and `persistence`."""
    obs = observed_cfs(cube, basin)
    cols = {}
    for label, (root, model) in runs.items():
        s = run_crps(Path(root) / f"site_id={site_id(basin)}" / f"{model}.parquet", obs, leads)
        if s is None:
            return None
        cols[label] = s
    df = pd.DataFrame(cols).dropna()
    if df.empty:
        return None
    issue = df.index.get_level_values("issue_time")
    lead = df.index.get_level_values("lead_h").to_numpy(float)
    last = obs.reindex(issue - pd.Timedelta(hours=1)).to_numpy(float)
    valid = obs.reindex(issue + pd.to_timedelta(lead, unit="h")).to_numpy(float)
    df["persistence"] = np.abs(last - valid)
    df["obs_issue"] = last
    df = df.dropna().reset_index()
    df.insert(0, "basin", basin)
    return df


def paired_table(cells: pd.DataFrame, labels: list[str], control: str, n_boot: int = 1000, block_days: int = 7, seed: int = 0) -> pd.DataFrame:
    """Per (basin, subset, lead): skill of each run vs persistence, and each run minus the control with a 95% interval."""
    rows = []
    for (basin, lead), g in cells.groupby(["basin", "lead_h"]):
        for subset, months in SUBSETS.items():
            s = g[g["issue_time"].dt.month.isin(months)]
            if len(s) < 20:
                continue
            days = s["issue_time"].dt.floor("D")
            day_index = pd.DatetimeIndex(sorted(days.unique()))
            pos = day_index.get_indexer(days)
            sums = {c: np.bincount(pos, weights=s[c].to_numpy(float), minlength=len(day_index)) for c in [*labels, "persistence"]}
            counts = block_bootstrap_counts(len(day_index), block_days, n_boot, np.random.default_rng(seed))
            pers, pers_b = sums["persistence"].sum(), counts @ sums["persistence"]
            row = {"basin": basin, "subset": subset, "lead_h": lead, "n": len(s)}
            for c in labels:
                row[f"crps_{c}"] = s[c].mean()
                row[f"skill_{c}"] = 1 - sums[c].sum() / pers
                if c == control:
                    continue
                diff = (sums[control] - sums[c]).sum() / pers
                boot = (counts @ (sums[control] - sums[c])) / pers_b
                lo, hi = np.quantile(boot, [0.025, 0.975])
                row[f"d_{c}"], row[f"d_{c}_lo"], row[f"d_{c}_hi"] = diff, lo, hi
            row["crps_persistence"] = s["persistence"].mean()
            rows.append(row)
    return pd.DataFrame(rows)


def paired_rows(table: pd.DataFrame, keys: list[str], labels: list[str], control: str, reference: str, n_boot: int = 1000, block_days: int = 7, seed: int = 0) -> pd.DataFrame:
    """Like `paired_table` for a long table with `crps_<label>` and `se_<label>` columns: per group of `keys`, mean
    CRPS and RMSE per label and each label minus the control (CRPS and RMSE) with 95% moving-block intervals."""
    rows = []
    for key, s in table.groupby(keys):
        if len(s) < 20:
            continue
        days = s["issue_time"].dt.floor("D")
        day_index = pd.DatetimeIndex(sorted(days.unique()))
        pos = day_index.get_indexer(days)
        counts = block_bootstrap_counts(len(day_index), block_days, n_boot, np.random.default_rng(seed))
        n_b = counts @ np.bincount(pos, minlength=len(day_index))
        sums = {f"{m}_{c}": np.bincount(pos, weights=s[f"{m}_{c}"].to_numpy(float), minlength=len(day_index)) for m in ("crps", "se") for c in labels}
        row = dict(zip(keys, key if isinstance(key, tuple) else (key,)), n=len(s))
        for c in labels:
            row[f"crps_{c}"] = s[f"crps_{c}"].mean()
            row[f"rmse_{c}"] = float(np.sqrt(s[f"se_{c}"].mean()))
        ref = sums[f"crps_{reference}"]
        for c in labels:
            row[f"skill_{c}"] = 1 - sums[f"crps_{c}"].sum() / ref.sum()
            if c == control:
                continue
            boot_crps = (counts @ (sums[f"crps_{control}"] - sums[f"crps_{c}"])) / n_b
            boot_rmse = np.sqrt((counts @ sums[f"se_{control}"]) / n_b) - np.sqrt((counts @ sums[f"se_{c}"]) / n_b)
            row[f"dcrps_{c}"] = row[f"crps_{control}"] - row[f"crps_{c}"]
            row[f"dcrps_{c}_lo"], row[f"dcrps_{c}_hi"] = np.quantile(boot_crps, [0.025, 0.975])
            row[f"drmse_{c}"] = row[f"rmse_{control}"] - row[f"rmse_{c}"]
            row[f"drmse_{c}_lo"], row[f"drmse_{c}_hi"] = np.quantile(boot_rmse, [0.025, 0.975])
        rows.append(row)
    return pd.DataFrame(rows)


def _wide_scores(fc: pd.DataFrame, obs: pd.Series) -> pd.DataFrame:
    """Per model and (issue, lead): CRPS of the members and squared error of their median, against `obs`."""
    out = {}
    for model, g in fc.groupby("model"):
        wide = g.pivot_table(index=["issue_time", "lead_h", "valid_time"], columns="member", values="value")
        o = obs.reindex(pd.DatetimeIndex(wide.index.get_level_values("valid_time"))).to_numpy(float)
        v = wide.to_numpy(float)
        idx = wide.index.droplevel("valid_time")
        out[f"crps_{model}"] = pd.Series(crps_ensemble(v, o), index=idx)
        out[f"se_{model}"] = pd.Series((np.nanmedian(v, axis=1) - o) ** 2, index=idx)
    return pd.DataFrame(out)


def temperature_cells(site: str, groups: dict[str, list[Path]], cube_paths: list[str], cal: pd.DataFrame | None, extra: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Water-temperature cells every model shares at one site: daily maxima at 12Z issues (days 0-7) and hourly values
    at 00/06/12/18Z (leads 1-168 h), with persistence (yesterday's maximum; the last hourly observation). `extra`:
    more daily-max forecasts (e.g. the USGS forecast) scored at their own issue times instead, with the flowcast runs."""
    basin = site.removeprefix("USGS-")
    tw = Cube(cube_paths).load_dynamic(basin, ["tw_c"], pd.Timestamp("2020-09-01"), FROZEN_TEST_START - pd.Timedelta(hours=1))["tw_c"].tz_localize("UTC")
    fc = load_forecasts(groups, site)
    if fc.empty or tw.dropna().empty:
        return {}
    fc = apply_calibration(fc[~fc["model"].str.endswith("_noflow")], cal)
    if fc.empty:
        return {}
    tw_max = daily_max(tw)
    out = {}
    daily = fc[(fc["variable"] == "water_temperature_daily_max") & (fc["issue_time"].dt.minute == 0) & (fc["issue_time"].dt.hour == 12)]
    hourly = fc[(fc["variable"] == "water_temperature") & (fc["issue_time"].dt.minute == 0) & fc["issue_time"].dt.hour.isin(ISSUE_HOURS) & fc["lead_h"].isin(LEADS_H)]
    for kind, part, obs in (("daily_max", daily, tw_max), ("hourly", hourly, tw)):
        wide = _wide_scores(part, obs)
        issue = wide.index.get_level_values("issue_time")
        lead = pd.to_timedelta(wide.index.get_level_values("lead_h").to_numpy(float), unit="h")
        if kind == "daily_max":
            local_day = issue.tz_convert("America/New_York").tz_localize(None).normalize().tz_localize("UTC")
            last = tw_max.reindex(local_day - pd.Timedelta(days=1)).to_numpy(float)
            valid = tw_max.reindex(local_day + lead).to_numpy(float)
        else:
            last = tw.reindex(issue - pd.Timedelta(hours=1)).to_numpy(float)
            valid = tw.reindex(issue + lead).to_numpy(float)
        wide["crps_persistence"] = np.abs(last - valid)
        wide["se_persistence"] = (last - valid) ** 2
        out[kind] = wide.dropna().reset_index().assign(site_id=site)
    if extra is not None and not extra.empty:
        issues = pd.DatetimeIndex(extra["issue_time"].unique())
        ours = fc[(fc["variable"] == "water_temperature_daily_max") & fc["issue_time"].isin(issues)]
        mine = _wide_scores(ours, tw_max)
        theirs = extra.pivot_table(index=["issue_time", "lead_h", "valid_time"], columns="quantile", values="value")
        o = tw_max.reindex(pd.DatetimeIndex(theirs.index.get_level_values("valid_time"))).to_numpy(float)
        levels = theirs.columns.to_numpy(float)
        idx = theirs.index.droplevel("valid_time")
        mine["crps_usgs"] = pd.Series(crps_quantiles(theirs.to_numpy(float), levels, o), index=idx)
        mine["se_usgs"] = pd.Series((theirs[0.5].to_numpy(float) - o) ** 2, index=idx)
        out["vs_usgs"] = mine.dropna().reset_index().assign(site_id=site)
    return out


def score_temperature_arms(groups: dict[str, list[str]], cube_paths: list[str], sites: list[str], out: str | Path, control: str, usgs_csv: str | None = None, n_boot: int = 1000, workers: int = 4) -> dict[str, pd.DataFrame]:
    """Paired water-temperature comparison of the groups' models against `control`, raw and calibrated (per-lead
    offset and spread factor, fitted on the other validation year and pooled over `sites`, as in tempscore)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    groups = {k: [Path(p) for p in v] for k, v in groups.items()}
    _, cal = fit_temperature_calibration(sites, groups, cube_paths, workers)
    cal.to_csv(out / "calibration.csv", index=False)
    usgs = lordville_usgs(usgs_csv) if usgs_csv else None
    with ProcessPoolExecutor(workers) as pool:
        parts = list(pool.map(temperature_cells, sites, [groups] * len(sites), [cube_paths] * len(sites), [cal] * len(sites), [usgs if s.endswith("01427207") else None for s in sites]))
    tables = {}
    for kind in ("daily_max", "hourly", "vs_usgs"):
        frames = [p[kind] for p in parts if kind in p]
        if not frames:
            continue
        cells = pd.concat(frames, ignore_index=True)
        cells.to_parquet(out / f"cells_{kind}.parquet", index=False)
        reference = "usgs" if kind == "vs_usgs" else "persistence"
        models = sorted({c.removeprefix("crps_") for c in cells.columns if c.startswith("crps_")} - {reference})
        summer = cells["issue_time"].dt.month.isin([6, 7, 8, 9])
        rows = []
        for calibrated, ctrl in ((False, control), (True, f"{control}_cal")):
            labels = [m for m in models if m.endswith("_cal") == calibrated] + [reference]
            for subset, part in (("all", cells), ("jun-sep", cells[summer])):
                rows.append(paired_rows(part, ["site_id", "lead_h"], labels, ctrl, reference, n_boot).assign(subset=subset, calibrated=calibrated))
        tables[kind] = pd.concat(rows, ignore_index=True)
        tables[kind].to_csv(out / f"paired_{kind}.csv", index=False)
    return tables


def score(runs: dict[str, tuple[str, str]], cube_paths: list[str], basins: list[str], out: str | Path, control: str, n_boot: int = 1000) -> pd.DataFrame:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cube = Cube(cube_paths)
    runs = {k: (Path(p), m) for k, (p, m) in runs.items()}
    frames = []
    for b in basins:
        df = basin_cells(b, runs, cube)
        if df is None:
            log.warning("skipping %s: missing hindcasts or no common cells", b)
            continue
        frames.append(df)
    cells = pd.concat(frames, ignore_index=True)
    cells.to_parquet(out / "cells.parquet", index=False)
    table = paired_table(cells, list(runs), control, n_boot=n_boot)
    table.to_csv(out / "paired.csv", index=False)
    return table
