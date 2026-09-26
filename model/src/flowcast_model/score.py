"""Score hindcasts from one or more runs with the evaluation harness, one site at a time.

Per site: every run's forecasts plus persistence, recession-persistence and climatology issued at the same times
(and the NWM v3.0 retrospective where the cube records an NWM reach), scored by `flowcast_eval.score_forecasts`
under the validation protocol (fit WY2001-2019, score WY2020-2022). Observations come from the cube's target,
converted to ft3/s and cut at the end of the validation years, so nothing from the frozen test years is used.

Writes per-site `scores.csv` / `vs_persistence.csv` rows (with a `site_id` column) and `summary.md` with
cross-basin medians per model and lead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from flowcast_eval.protocol import VALIDATION, HindcastProtocol
from flowcast_eval.schema import normalize_forecasts
from flowcast_eval.scoreboard import score_against_references, site_or_stub

from .cube import FROZEN_TEST_START, Cube, CubeDims
from .units import to_cfs

log = logging.getLogger(__name__)
REPORT_LEADS = [1, 6, 12, 24, 48, 72, 120, 168]


def _site_files(paths: list[Path]) -> dict[str, list[Path]]:
    files: dict[str, list[Path]] = {}
    for root in paths:
        for part in sorted(Path(root).glob("site_id=*")):
            files.setdefault(part.name.split("=", 1)[1], []).extend(sorted(part.glob("*.parquet")))
    return files


NLDI_SITE = "https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{}"


def nwm_reaches(basins: list[str], cache: Path | None = None) -> dict[str, int]:
    """NHDPlus v2 COMID (= NWM v3 feature_id) of each gauge from the NLDI, cached as JSON."""
    cache = cache or Path.home() / ".cache" / "flowcast" / "nwm_reaches.json"
    known = json.loads(cache.read_text()) if cache.exists() else {}
    session = requests.Session()
    for b in basins:
        if b in known:
            continue
        try:
            known[b] = int(session.get(NLDI_SITE.format(b), timeout=60).json()["features"][0]["properties"]["comid"])
        except Exception as err:  # the reference is optional; score without it
            log.warning("no NLDI COMID for %s: %s", b, err)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(known))
    return {b: known[b] for b in basins if b in known}


def cube_obs_cfs(cube: Cube, basin: str, target: str, unit: str, area_km2: float | None, end: pd.Timestamp) -> pd.Series:
    df = cube.load_dynamic(basin, [target], pd.Timestamp("2000-10-01"), end)
    values = to_cfs(df[target].to_numpy(np.float64), unit, area_km2)
    return pd.Series(values, index=df.index.tz_localize("UTC"), name="discharge")


def score_runs(
    forecast_dirs: list[str | Path],
    cube_paths: list[str],
    out: str | Path,
    target: str,
    unit: str = "mm/h",
    area_attribute: str | None = "area_km2",
    nwm_attribute: str | None = "nwm_feature_id",
    dims: dict | None = None,
    protocol: HindcastProtocol = VALIDATION,
    n_boot: int = 1000,
) -> pd.DataFrame:
    end = min(protocol.test_window[1].tz_localize(None), FROZEN_TEST_START - pd.Timedelta(hours=1))
    cube = Cube(cube_paths, CubeDims.from_dict(dims))
    protocol = replace(protocol, n_boot=n_boot)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    files = _site_files([Path(p) for p in forecast_dirs])
    basins = [s.removeprefix("USGS-") for s in files]
    attrs = [a for a in (area_attribute, nwm_attribute) if a and cube.has(a)]
    static = cube.load_static([b for b in basins if b in set(cube.basins)], attrs) if attrs else pd.DataFrame()
    reaches = {} if (nwm_attribute and nwm_attribute in static) or not nwm_attribute else nwm_reaches(basins)
    all_scores, all_paired = [], []
    for sid, paths in files.items():
        basin = sid.removeprefix("USGS-")
        frames = []
        for p in paths:
            f = pd.read_parquet(p)
            f["site_id"] = sid
            frames.append(f)
        forecasts = normalize_forecasts(pd.concat(frames, ignore_index=True))
        forecasts = forecasts[forecasts["valid_time"] <= pd.Timestamp(end, tz="UTC")]
        area = float(static.loc[basin, area_attribute]) if area_attribute in static else None
        obs = cube_obs_cfs(cube, basin, target, unit, area, end)
        reach = static.loc[basin, nwm_attribute] if nwm_attribute and nwm_attribute in static else reaches.get(basin, np.nan)
        reach = int(reach) if np.isfinite(reach) and reach > 0 else None
        try:
            res = score_against_references(forecasts, obs, site_or_stub(sid), protocol, nwm_reach=reach, references=("persistence", "nwm_retrospective"))
        except Exception:
            log.exception("scoring failed for %s", sid)
            continue
        for key, frame in res.items():
            frame.insert(0, "site_id", sid)
        all_scores.append(res["scores"])
        all_paired.append(pd.concat([v for k, v in res.items() if k.startswith("vs_")], ignore_index=True))
        log.info("scored %s (%d forecast rows)", sid, len(forecasts))
    scores = pd.concat(all_scores, ignore_index=True)
    paired = pd.concat(all_paired, ignore_index=True)
    scores.to_csv(out / "scores.csv", index=False)
    paired.to_csv(out / "paired.csv", index=False)
    summary = summarize(scores, paired)
    summary.to_csv(out / "summary.csv", index=False)
    (out / "summary.md").write_text(render_summary(summary, scores, protocol))
    return summary


def summarize(scores: pd.DataFrame, paired: pd.DataFrame) -> pd.DataFrame:
    """Cross-basin medians per model and lead: CRPS skill vs persistence/NWM retrospective, KGE, NSE."""
    rows = []
    for (model, lead), g in scores.groupby(["model", "lead_h"]):
        val = lambda m: g.loc[g["metric"] == m, "value"]  # noqa: E731
        row = {"model": model, "lead_h": lead, "sites": int(g["site_id"].nunique()), "kge_median": val("kge").median(), "nse_median": val("nse").median(), "crps_median": val("crps").median()}
        for ref in ("persistence", "nwm_retrospective"):
            p = paired[(paired["model"] == model) & (paired["reference"] == ref) & (paired["lead_h"] == lead) & (paired["metric"] == "crps")]
            row[f"crpss_vs_{ref}_median"] = p["skill"].median() if len(p) else np.nan
            row[f"sites_better_than_{ref}"] = int((p["better"] == True).sum()) if len(p) else 0  # noqa: E712
            row[f"sites_worse_than_{ref}"] = int((p["better"] == False).sum()) if len(p) else 0  # noqa: E712
        rows.append(row)
    return pd.DataFrame(rows)


def render_summary(summary: pd.DataFrame, scores: pd.DataFrame, protocol: HindcastProtocol) -> str:
    def pivot(col: str, fmt: str) -> str:
        t = summary[summary["lead_h"].isin(REPORT_LEADS)].pivot(index="model", columns="lead_h", values=col)
        header = "| model | " + " | ".join(f"{int(c)} h" for c in t.columns) + " |"
        sep = "|---|" + "---|" * len(t.columns)
        body = ["| " + m + " | " + " | ".join("–" if pd.isna(v) else fmt.format(v) for v in r) + " |" for m, r in zip(t.index, t.to_numpy())]
        return "\n".join([header, sep, *body])

    n_sites = scores["site_id"].nunique()
    return "\n".join(
        [
            f"# Validation scoreboard ({protocol.test_start} to {protocol.test_end})",
            "",
            f"{n_sites} basins; issues at {', '.join(f'{h:02d}Z' for h in protocol.issue_hours_utc)}; cross-basin medians. "
            "Frozen test years (WY2023+) are not touched. `nwm_retrospective` is a perfect-forcing simulation (no data assimilation); "
            "flowcast runs on reanalysis forcing are perfect-forcing too.",
            "",
            "## Median CRPS skill vs persistence (1 - CRPS/CRPS_persistence)",
            "",
            pivot("crpss_vs_persistence_median", "{:+.2f}"),
            "",
            "## Median CRPS skill vs NWM retrospective",
            "",
            pivot("crpss_vs_nwm_retrospective_median", "{:+.2f}"),
            "",
            "## Median KGE",
            "",
            pivot("kge_median", "{:.3f}"),
            "",
            "## Median NSE",
            "",
            pivot("nse_median", "{:.3f}"),
            "",
            "## Sites significantly better than persistence (CRPS, 95% block bootstrap)",
            "",
            pivot("sites_better_than_persistence", "{:.0f}"),
            "",
            "## Sites significantly worse than persistence",
            "",
            pivot("sites_worse_than_persistence", "{:.0f}"),
            "",
        ]
    )
