"""Lead-dependent blend of the LSTM ensemble with persistence, fitted and scored without leakage.

Each member becomes `w_l * lstm + (1 - w_l) * persistence` at lead `l`, where persistence is the last observation
available at issue time (1 h latency, as in the harness). The weights are fitted on one validation water year and
scored on the other (two folds), so no year is scored with weights fitted on it; the frozen test years are never used.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_eval.metrics import crps_ensemble

from .cube import Cube
from .units import to_cfs

WEIGHTS = np.round(np.linspace(0, 1, 21), 2)
FOLDS = {"WY2021": ("2020-10-01", "2021-09-30T23:00"), "WY2022": ("2021-10-01", "2022-09-30T23:00")}


def _site_arrays(path: Path, cube: Cube, basin: str, area: float, leads: list[float]) -> dict:
    f = pd.read_parquet(path)
    f = f[f["issue_time"].dt.minute.eq(0) & f["issue_time"].dt.hour.isin([0, 6, 12, 18]) & f["lead_h"].isin(leads)]
    piv = f.pivot_table(index=["issue_time", "lead_h"], columns="member", values="value")
    q = cube.load_dynamic(basin, ["qobs_mm_h"], pd.Timestamp("2020-09-01"), pd.Timestamp("2022-09-30T23:00"))["qobs_mm_h"]
    q = pd.Series(to_cfs(q.to_numpy(np.float64), "mm/h", area), index=q.index.tz_localize("UTC"))
    issues = piv.index.get_level_values(0)
    lead = piv.index.get_level_values(1).to_numpy(float)
    valid = issues + pd.to_timedelta(lead, unit="h")
    return {
        "issue": issues,
        "lead": lead,
        "ens": piv.to_numpy(float),
        "obs": q.reindex(valid).to_numpy(),
        "pers": q.reindex(issues - pd.Timedelta(hours=1)).to_numpy(),
    }


def _crps_grid(a: dict) -> np.ndarray:
    """CRPS [n, len(WEIGHTS)] of the blended ensemble; column 0 is persistence, the last column the raw LSTM."""
    out = np.full((len(a["obs"]), len(WEIGHTS)), np.nan)
    for j, w in enumerate(WEIGHTS):
        ens = w * a["ens"] + (1 - w) * a["pers"][:, None]
        out[:, j] = crps_ensemble(ens, a["obs"])
    return out


def blend_experiment(hindcast_dir: str | Path, cube_paths: list[str], model: str, leads=(1, 3, 6, 12, 18, 24, 48, 72)) -> tuple[pd.DataFrame, pd.DataFrame]:
    cube = Cube(cube_paths)
    leads = [float(x) for x in leads]
    parts = sorted(Path(hindcast_dir).glob(f"site_id=*/{model}.parquet"))
    basins = [p.parent.name.split("=", 1)[1].removeprefix("USGS-") for p in parts]
    areas = cube.load_static(basins, ["area_km2"])["area_km2"]
    per_site = {}
    for p, b in zip(parts, basins):
        a = _site_arrays(p, cube, b, float(areas[b]), leads)
        a["crps"] = _crps_grid(a)
        per_site[b] = a
    # skill vs persistence per site, fold, lead and weight
    rows = []
    for b, a in per_site.items():
        for fold, (s, e) in FOLDS.items():
            in_fold = (a["issue"] >= pd.Timestamp(s, tz="UTC")) & (a["issue"] <= pd.Timestamp(e, tz="UTC"))
            for lead in leads:
                m = in_fold & (a["lead"] == lead) & np.isfinite(a["obs"]) & np.isfinite(a["pers"])
                if m.sum() < 50:
                    continue
                c = np.nanmean(a["crps"][m], axis=0)
                rows.append({"basin": b, "fold": fold, "lead_h": lead, **{f"w{w}": 1 - c[j] / c[0] for j, w in enumerate(WEIGHTS)}})
    skill = pd.DataFrame(rows)
    wcols = [f"w{w}" for w in WEIGHTS]
    chosen, scored = [], []
    for fold in FOLDS:
        other = [f for f in FOLDS if f != fold][0]
        for lead in leads:
            fit = skill[(skill["fold"] == other) & (skill["lead_h"] == lead)]
            best = wcols[int(np.argmax(fit[wcols].mean().to_numpy()))]
            test = skill[(skill["fold"] == fold) & (skill["lead_h"] == lead)]
            chosen.append({"scored_fold": fold, "lead_h": lead, "weight_on_lstm": float(best[1:])})
            scored.append(test[["basin", "fold", "lead_h"]].assign(blend=test[best].to_numpy(), lstm=test["w1.0"].to_numpy()))
    scored = pd.concat(scored, ignore_index=True)
    summary = scored.groupby("lead_h").agg(lstm_median=("lstm", "median"), blend_median=("blend", "median"), sites=("basin", "nunique")).reset_index()
    return summary, pd.DataFrame(chosen)
