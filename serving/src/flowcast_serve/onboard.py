"""Site onboarding for model basins: everything a forecast run needs that doesn't change between runs.

Per basin, into `sites/USGS-{id}/serving/` in the lake:
- `weights/{hrrr_analysis,mrms,gefs_forecast,gefs_forecast_bands}.npz`: the basin's slice of the cube's extraction
  plans (`flowcast-training-…-us-east-2/work/runs/{r1,rf1}/plans/`);
- `weights/snodas.npz`: its AORC basin and band rows (`work/meta/weights_aorc_all.npz`) remapped to the SNODAS grid;
- `hrus/`: SNOW-17 HRUs (`flowcast_pipeline.snow.load_or_build_hrus`: NLDI basin, terrain, WorldCover);
- `site.json`: NWPS flood categories (stage and flow) where the site has an NWS forecast point.
Static attributes come from the model version (`statics.parquet`), so onboarding a cube basin needs no model work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests
import scipy.sparse as sp
from flowcast_pipeline.dataset.extract import Plan
from flowcast_pipeline.dataset.snodas import GRID, remap_weights
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.snow import load_or_build_hrus

from . import forcing, snodas_live
from .registry import ServedSite

log = logging.getLogger(__name__)

PLAN_SOURCES = ("hrrr_analysis", "mrms", "gefs_forecast", "gefs_forecast_bands")
NWPS_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
N_BANDS = 4


def nwps_categories(lid: str) -> list[dict]:
    r = requests.get(NWPS_URL.format(lid=lid), timeout=30)
    r.raise_for_status()
    cats = (r.json().get("flood") or {}).get("categories") or {}
    out = []
    for name in ("action", "minor", "moderate", "major"):
        c = cats.get(name) or {}
        stage, flow = c.get("stage"), c.get("flow")
        if stage is not None and stage > -9000:
            out.append({"category": name, "stage_ft": float(stage), "flow_cfs": float(flow) if flow is not None and flow > 0 else None})
    return out


def onboard(lake: Lake, basins: list[str], plan_dir: Path, meta_dir: Path, hru_cache: Path, sites: dict[str, ServedSite], build_hrus: bool = False) -> dict:
    plans = {s: Plan.load(plan_dir / f"{s}.pkl") for s in PLAN_SOURCES}
    w_all = sp.load_npz(meta_dir / "weights_aorc_all.npz").tocsr()
    units = json.loads((meta_dir / "aorc_units.json").read_text())
    upos = {u: i for i, u in enumerate(units)}
    report = {"weights": 0, "hrus": 0, "hrus_missing": [], "nwps": 0}
    for b in basins:
        for source, plan in plans.items():
            lake.write(forcing.weights_key(b, source), forcing.SiteWeights.from_plan(plan, b).to_bytes(), "application/octet-stream")
        rows = [upos[b], *[upos[f"{b}/band{k}"] for k in range(N_BANDS)]]
        snodas_live.save_weights(lake, b, remap_weights(w_all[rows], dst=GRID))
        report["weights"] += 1
        hru_dir = hru_cache / f"USGS-{b}"
        if build_hrus and not (hru_dir / "hrus.parquet").exists():
            try:
                load_or_build_hrus(f"USGS-{b}", hru_cache)
            except Exception as err:
                log.warning("HRUs for %s failed: %s", b, err)
        if (hru_dir / "hrus.parquet").exists():
            for p in hru_dir.iterdir():
                if p.is_file():
                    lake.write(f"sites/USGS-{b}/serving/hrus/{p.name}", p.read_bytes())
            report["hrus"] += 1
        else:
            report["hrus_missing"].append(b)
        site = sites.get(f"USGS-{b}")
        info = {"nws_lid": site.nws_lid if site else None, "flood_categories": []}
        if site and site.nws_lid:
            try:
                info["flood_categories"] = nwps_categories(site.nws_lid)
                report["nwps"] += 1
            except requests.RequestException as err:
                log.warning("NWPS %s: %s", site.nws_lid, err)
        lake.write(f"sites/USGS-{b}/serving/site.json", json.dumps(info).encode(), "application/json")
    return report
