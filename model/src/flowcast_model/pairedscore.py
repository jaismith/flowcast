"""Paired streamflow comparison of runs against a control on the cells every run forecasts (validation years).

For each basin: fair ensemble CRPS (ft3/s) of each run's members at 00/06/12/18Z issues and the given leads,
persistence (the observation 1 h before issue), and skill = 1 - CRPS / CRPS(persistence). Every run and persistence
are scored on the same (issue, lead) cells. Differences against the control come with a paired moving-block
bootstrap over issue days (7-day blocks). Subsets are issue months, e.g. the summer low-flow season.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_eval.metrics import crps_ensemble
from flowcast_eval.scoring import block_bootstrap_counts

from .cube import FROZEN_TEST_START, Cube
from .hindcast import site_id
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
