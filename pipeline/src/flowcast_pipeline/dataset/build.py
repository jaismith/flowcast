"""Steps of the cube v1 build, wired together for the CLI. Intermediate files live under `work_dir()`."""

import json
import logging
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import scipy.sparse as sp

from ..usgs.client import WaterDataClient
from ..usgs.params import Parameter
from . import basins, camelsh, config, cube, extract, fleet, regulation, sources, targets, terrain, weights
from .inventory import continuous_inventory

log = logging.getLogger(__name__)

N_BANDS = 4
PLAN_SOURCES = ("aorc", "hrrr_analysis", "mrms", "hrrr_forecast", "gefs_forecast")


def camelsh_dir(root: Path) -> Path:
    return root.parent / "camelsh"


def load_selection(root: Path) -> tuple[list[str], list[str], dict[str, list[str]]]:
    sel = list(pd.read_parquet(root / "selection.parquet").index)
    return sel, json.loads((root / "slice.json").read_text()), json.loads((root / "outflows.json").read_text())


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    cam = camelsh.download(camelsh_dir(root))
    attrs = camelsh.attributes(cam).copy()
    polys = camelsh.boundaries(cam)
    info = pd.read_csv(cam / "info.csv", dtype={"STAID": str})
    client = WaterDataClient()
    inv_q = continuous_inventory(client, Parameter.DISCHARGE)
    inv_tw = continuous_inventory(client, Parameter.WATER_TEMPERATURE)
    inv_q.to_parquet(root / "inventory_q.parquet")
    inv_tw.to_parquet(root / "inventory_tw.parquet")
    now = pd.Timestamp.now(tz="UTC")

    cand = basins.eligible(attrs, inv_q, info, now)
    sel = basins.select(cand, set(inv_tw.loc[inv_tw["end"] >= basins.ACTIVE_SINCE, "site"]))
    sel.to_parquet(root / "selection.parquet")
    log.info("selected %d of %d eligible basins", len(sel), len(cand))

    huc = attrs["HUC02"].astype(str).str.zfill(2)
    active_q = inv_q.set_index("site")
    outflow_candidates = [
        s for s in attrs.index[huc.isin(config.HUC2) & (attrs["DRAIN_SQKM"] >= 10)]
        if s in active_q.index and active_q.loc[s, "end"] >= pd.Timestamp("2020-01-01", tz="UTC") and s in polys.index
    ]
    dams = regulation.load_nid(regulation.download_nid(root.parent / "nid" / "nation.csv"))
    reg, outflows = regulation.regulation_table(list(sel.index), outflow_candidates, attrs, polys, dams)
    reg.to_parquet(root / "regulation.parquet")
    (root / "outflows.json").write_text(json.dumps(outflows))
    early = basins.early_slice(sel, reg)
    (root / "slice.json").write_text(json.dumps(early))

    build_plans(root, list(sel.index), early, polys)


def build_plans(root: Path, sel: list[str], early: list[str], polys) -> None:
    plan_dir = root / "plans"
    plan_dir.mkdir(exist_ok=True)
    geoms = polys.loc[sel].geometry
    slice_idx = np.array([sel.index(s) for s in early])

    w_aorc = weights.basin_weights(geoms, sources.grid_for(sources.SOURCES["aorc"]), sources.SOURCES["aorc"].supersample)
    cells = np.unique(w_aorc.indices)
    tiles = terrain_tiles(cells)
    paths = terrain.build(tiles, root / "terrain")
    layers = terrain.assemble(paths, cells)
    elev = np.full(w_aorc.shape[1], np.nanmean(layers[0]), dtype=np.float32)
    elev[cells] = np.where(np.isnan(layers[0]), np.nanmean(layers[0]), layers[0])
    w_bands, share = weights.band_weights(w_aorc, elev, N_BANDS)
    np.savez(root / "aorc_cell_terrain.npz", cells=cells, layers=layers, band_share=share)
    w_all = sp.vstack([w_aorc, w_bands]).tocsr()
    sp.save_npz(root / "weights_aorc_all.npz", w_all)
    units = sel + [f"{b}/band{k}" for b in sel for k in range(N_BANDS)]
    (root / "aorc_units.json").write_text(json.dumps(units))
    slice_units = np.concatenate([slice_idx, [len(sel) + i * N_BANDS + k for i in slice_idx for k in range(N_BANDS)]])
    extract.make_plan("aorc", w_all, units, slice_units, config.TIME_START, config.TIME_END).save(plan_dir / "aorc.pkl")

    for name in PLAN_SOURCES[1:]:
        src = sources.SOURCES[name]
        w = weights.basin_weights(geoms, sources.grid_for(src), src.supersample)
        extract.make_plan(name, w, sel, slice_idx, config.TIME_START, config.TIME_END).save(plan_dir / f"{name}.pkl")
        log.info("plan %s ready", name)


def terrain_tiles(cells: np.ndarray) -> list[tuple[int, int]]:
    g = sources.grid_for(sources.SOURCES["aorc"])
    iy, ix = np.divmod(cells, g.nx)
    lat = np.floor(g.y0 + g.dy * iy).astype(int)
    lon = np.floor(g.x0 + g.dx * ix).astype(int)
    return sorted(set(zip(lat.tolist(), lon.tolist())))


def pull_targets(root: Path) -> None:
    """USGS pulls in priority order: early-slice sites first, then the rest.

    Discharge comes from CAMELSH through 2023 and from the API from 2024 on, except for always-included
    sites and outflow gauges without a CAMELSH record, which are pulled in full. Water temperature is
    always pulled in full.
    """
    sel, early, outflows = load_selection(root)
    info = pd.read_csv(camelsh_dir(root) / "info.csv", dtype={"STAID": str}).set_index("STAID")
    cam_years = info[[str(y) for y in range(2000, 2025)]].sum(axis=1) / 8766
    inv_q = pd.read_parquet(root / "inventory_q.parquet").set_index("site")
    inv_tw = pd.read_parquet(root / "inventory_tw.parquet").set_index("site")

    def q_start(s: str) -> pd.Timestamp:
        full = s in config.ALWAYS_INCLUDE or cam_years.get(s, 0) < 5
        return max(inv_q.loc[s, "begin"].floor("D"), config.TIME_START) if full else config.USGS_TARGETS_FROM

    first = sorted(set(early) | {g for b in early for g in outflows.get(b, [])})
    rest = [s for s in sorted(set(sel) | {g for v in outflows.values() for g in v}) if s not in first]
    jobs = []
    for group in (first, rest):
        jobs += [(s, "discharge", q_start(s)) for s in group if s in inv_q.index]
        jobs += [(s, "water_temperature", max(inv_tw.loc[s, "begin"].floor("D"), config.TIME_START)) for s in group if s in inv_tw.index]
    results = targets.pull_all(jobs, pd.Timestamp.now(tz="UTC"), root / "targets", root.parent / "usgs-cache")
    log.info("targets done: %d failures", sum(v < 0 for v in results.values()))


def launch_extraction(root: Path, run: str, n_instances: int, workers: int, max_minutes: int, dry_run: bool) -> None:
    plan_dir = root / "plans"
    plans = {name: extract.Plan.load(plan_dir / f"{name}.pkl") for name in PLAN_SOURCES}
    done = fleet.done_shards(run) | fleet.active_shards(run, plans)
    assignments = fleet.assign(plans, n_instances, skip=done)
    for a in assignments:
        by_src = pd.Series([s for s, _ in a.jobs]).value_counts().to_dict()
        log.info("instance %d: est %.0f core-min, %.1f GB accumulators, shards %s", a.index, a.cost_s / 60, a.mem_gb, by_src)
    if dry_run or not assignments:
        return
    repo_root = Path(__file__).resolve().parents[4]
    bundle = root / fleet.bundle_code(repo_root, root)
    ids = fleet.launch(run, assignments, plan_dir, bundle, workers, max_minutes)
    log.info("launched %s", ids)


def status(run: str) -> None:
    ec2 = boto3.client("ec2", region_name=config.AWS_REGION)
    res = ec2.describe_instances(Filters=[{"Name": "tag:run", "Values": [run]}, {"Name": "tag:component", "Values": ["dataset"]}])
    for r in res["Reservations"]:
        for inst in r["Instances"]:
            print(inst["InstanceId"], inst["InstanceType"], inst["State"]["Name"], inst.get("LaunchTime"))
    done = fleet.done_shards(run)
    print(f"{len(done)} shards complete")


def assemble(root: Path, run: str, subset: str, upload: bool) -> None:
    cube.assemble_cube(root, run, subset, upload)
