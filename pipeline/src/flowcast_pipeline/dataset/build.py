"""Steps of the cube v1 build, wired together for the CLI. Intermediate files live under `work_dir()`."""

import json
import logging
from pathlib import Path

import boto3
import numpy as np
import requests
import pandas as pd
import scipy.sparse as sp

from ..usgs.client import WaterDataClient
from ..usgs.params import Parameter
from . import basins, camelsh, config, cube, extract, fleet, reforecast, regulation, sources, targets, terrain, traveltime, upstream, weights
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
    # Some series are listed with no data period (NaT begin); there is nothing to pull for them.
    inv_q = pd.read_parquet(root / "inventory_q.parquet").dropna(subset=["begin"]).set_index("site")
    inv_tw = pd.read_parquet(root / "inventory_tw.parquet").dropna(subset=["begin"]).set_index("site")

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


# ------------------------------------------------------------------------------ v1.1: GEFSv12 reforecast

RF_INSTANCE_TYPES = ("c7i.2xlarge", "c6i.2xlarge", "c7a.2xlarge", "m7i.2xlarge", "m6i.2xlarge")
META_FILES = ("selection.parquet", "slice.json", "aorc_units.json", "outflows.json")


def prepare_reforecast(root: Path) -> None:
    """Plans for the reforecast (basins + bands on its 0-360 grid) and for operational GEFS elevation bands."""
    sel, early, _ = load_selection(root)
    polys = camelsh.boundaries(camelsh_dir(root))
    units = json.loads((root / "aorc_units.json").read_text())
    w_aorc_all = sp.load_npz(root / "weights_aorc_all.npz").tocsr()
    aorc = sources.grid_for(sources.SOURCES["aorc"])

    w_basins = weights.basin_weights(polys.loc[sel].geometry, reforecast.RF_GRID, sources.SOURCES["gefs_forecast"].supersample)
    w_bands = reforecast.gefs_band_weights(w_aorc_all, len(sel), aorc, reforecast.RF_GRID)
    plan = reforecast.make_plan(units, sp.vstack([w_basins, w_bands]).tocsr())
    pd.to_pickle(plan, root / "plans" / "gefs_reforecast.pkl")

    band_units = units[len(sel):]
    w_op_bands = reforecast.gefs_band_weights(w_aorc_all, len(sel), aorc, sources.grid_for(sources.SOURCES["gefs_forecast"]))
    extract.make_plan("gefs_forecast_bands", w_op_bands, band_units, np.array([], dtype=int), config.TIME_START, config.TIME_END).save(
        root / "plans" / "gefs_forecast_bands.pkl"
    )
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    for name in META_FILES:
        s3.upload_file(str(root / name), config.BUCKET, f"work/meta/{name}")
    log.info("reforecast plan: %d units over %d grid cells", len(plan.units), len(plan.cells))


def launch_reforecast(root: Path, run: str, rf_instances: int, band_instances: int, workers: int, max_minutes: int) -> None:
    plan_dir = root / "plans"
    done = fleet.done_shards(run)
    months = [m for m in reforecast.month_shards() if ("gefs_reforecast", m) not in done]
    repo_root = Path(__file__).resolve().parents[4]
    bundle = root / fleet.bundle_code(repo_root, root)
    ids: list[str] = []
    if rf_instances and months:
        rf_assign = [fleet.Assignment(k, months[k::rf_instances], 0.0, 0.0) for k in range(rf_instances) if months[k::rf_instances]]
        ids += fleet.launch(run, rf_assign, plan_dir, bundle, workers, max_minutes, kind="reforecast", instance_types=RF_INSTANCE_TYPES)
    if band_instances:
        plans = {"gefs_forecast_bands": extract.Plan.load(plan_dir / "gefs_forecast_bands.pkl")}
        band_assign = fleet.assign(plans, band_instances, skip=done)
        ids += fleet.launch(run, band_assign, plan_dir, bundle, 16, max_minutes, upload_plans=not ids)
    log.info("launched %s", ids)


def launch_assemble_v11(root: Path, run: str, max_minutes: int, spot: bool = True) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    bundle = root / fleet.bundle_code(repo_root, root)
    command = f"assemble-v11 --run {run} --subset slice50 full"
    ids = fleet.launch(
        f"{run}-assemble", [fleet.Assignment(0, [], 0.0, 0.0)], root / "plans", bundle, 1, max_minutes,
        kind="assemble", command=command, instance_types=fleet.INSTANCE_TYPES, volume_gb=450, upload_plans=False, spot=spot,
    )
    log.info("assembler %s", ids)


def assemble_v11(root: Path, run: str, subsets: list[str]) -> None:
    for subset in subsets:
        cube.add_v11(root, run, subset)


# ------------------------------------------------------------------------------ v1.2: upstream gauges


def discover_upstream(root: Path) -> None:
    """NLDI upstream-gauge discovery for every basin (slice first), written to `upstream.json`."""
    sel = pd.read_parquet(root / "selection.parquet")
    early = json.loads((root / "slice.json").read_text())
    order = early + [s for s in sel.index if s not in early]
    vaa = root.parent / "nhdplus" / "vaa.parquet"
    if not vaa.exists():
        vaa.parent.mkdir(parents=True, exist_ok=True)
        vaa.write_bytes(requests.get(upstream.VAA_URL, timeout=900).content)
    net = upstream.Network.load(vaa)
    inv = pd.read_parquet(root / "inventory_q.parquet").set_index("site")
    found = upstream.discover(order, root / "nldi", net, inv, sel["DRAIN_SQKM"].astype(float))
    (root / "upstream.json").write_text(json.dumps(found))
    log.info("%d of %d basins have upstream gauges", sum(1 for v in found.values() if v), len(found))


def pull_upstream_targets(root: Path) -> None:
    """Discharge for the outermost upstream gauges: CAMELSH through 2023 where it exists, else the full USGS record."""
    up = json.loads((root / "upstream.json").read_text())
    early = json.loads((root / "slice.json").read_text())
    info = pd.read_csv(camelsh_dir(root) / "info.csv", dtype={"STAID": str}).set_index("STAID")
    cam_years = info[[str(y) for y in range(2000, 2025)]].sum(axis=1) / 8766
    inv = pd.read_parquet(root / "inventory_q.parquet").set_index("site")

    def outermost(basins: list[str]) -> list[str]:
        return sorted({g["site"] for b in basins for g in up.get(b, []) if g["outermost"]})

    first = outermost(early)
    rest = [s for s in outermost(list(up)) if s not in first]
    for group in (first, rest):
        jobs = [
            (s, "discharge", config.USGS_TARGETS_FROM if cam_years.get(s, 0) >= 5 else max(inv.loc[s, "begin"].floor("D"), config.TIME_START))
            for s in group
        ]
        results = targets.pull_all(jobs, pd.Timestamp.now(tz="UTC"), root / "targets", root.parent / "usgs-cache", workers=10, min_interval_s=50.0, site_timeout_s=1800.0)
        log.info("upstream targets: %d jobs, %d failed", len(jobs), sum(v < 0 for v in results.values()))


def assemble_v12(root: Path, subsets: list[str]) -> None:
    for subset in subsets:
        cube.add_v12(root, subset)


# ------------------------------------------------------------------------------ v1.3: travel-time zones

V13_META = ("weights_aorc_tz3.npz", "weights_aorc_tz1h.npz", "traveltime_summary.parquet", "weights_aorc_all.npz")


def zone_units(sel: list[str]) -> tuple[list[str], list[str]]:
    coarse = [f"{b}/tz3_{k}" for b in sel for k in range(traveltime.N_COARSE)]
    hourly = [f"{b}/tz1h_{k}" for b in sel for k in range(traveltime.N_HOURLY)]
    return coarse, hourly


def prepare_zones(root: Path) -> Path:
    """Travel-time zone weights (if not built) and v1.3 plans: AORC zones, operational and reforecast GEFS coarse zones."""
    sel, early, _ = load_selection(root)
    aorc = sources.grid_for(sources.SOURCES["aorc"])
    if not (root / "weights_aorc_tz1h.npz").exists():
        net = upstream.Network.load(root.parent / "nhdplus" / "vaa.parquet")
        w_basins = sp.load_npz(root / "weights_aorc_all.npz").tocsr()[: len(sel)]
        wc, wh, summary = traveltime.zone_weights(sel, w_basins, aorc, root / "nldi", net)
        sp.save_npz(root / "weights_aorc_tz3.npz", wc)
        sp.save_npz(root / "weights_aorc_tz1h.npz", wh)
        summary.to_parquet(root / "traveltime_summary.parquet")
    wc = sp.load_npz(root / "weights_aorc_tz3.npz").tocsr()
    wh = sp.load_npz(root / "weights_aorc_tz1h.npz").tocsr()
    coarse, hourly = zone_units(sel)
    idx = [sel.index(b) for b in early]
    slice_units = np.array(
        [i * traveltime.N_COARSE + k for i in idx for k in range(traveltime.N_COARSE)]
        + [len(coarse) + i * traveltime.N_HOURLY + k for i in idx for k in range(traveltime.N_HOURLY)]
    )
    plan_dir = root / "plans_v13"
    plan_dir.mkdir(exist_ok=True)
    extract.make_plan("aorc_zones", sp.vstack([wc, wh]).tocsr(), coarse + hourly, slice_units, config.TIME_START, config.TIME_END).save(plan_dir / "aorc_zones.pkl")
    gefs = sources.grid_for(sources.SOURCES["gefs_forecast"])
    extract.make_plan(
        "gefs_forecast_zones", reforecast.aggregate_to_grid(wc, aorc, gefs), coarse, np.array([], dtype=int), config.TIME_START, config.TIME_END
    ).save(plan_dir / "gefs_forecast_zones.pkl")
    pd.to_pickle(reforecast.make_plan(coarse, reforecast.aggregate_to_grid(wc, aorc, reforecast.RF_GRID)), plan_dir / "gefs_reforecast.pkl")
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    for name in (*META_FILES, *V13_META):
        s3.upload_file(str(root / name), config.BUCKET, f"work/meta/{name}")
    log.info("v1.3 plans ready in %s", plan_dir)
    return plan_dir


def launch_zones(root: Path, run: str, aorc_instances: int, rf_instances: int, max_minutes: int) -> None:
    plan_dir = root / "plans_v13"
    done = fleet.done_shards(run)
    repo_root = Path(__file__).resolve().parents[4]
    bundle = root / fleet.bundle_code(repo_root, root)
    plans = {n: extract.Plan.load(plan_dir / f"{n}.pkl") for n in ("aorc_zones", "gefs_forecast_zones")}
    ids = fleet.launch(run, fleet.assign(plans, aorc_instances, skip=done), plan_dir, bundle, 16, max_minutes)
    months = [m for m in reforecast.month_shards() if ("gefs_reforecast", m) not in done]
    if rf_instances and months:
        rf_assign = [fleet.Assignment(k, months[k::rf_instances], 0.0, 0.0) for k in range(rf_instances) if months[k::rf_instances]]
        ids += fleet.launch(run, rf_assign, plan_dir, bundle, 12, max_minutes, kind="reforecast", instance_types=RF_INSTANCE_TYPES, upload_plans=False)
    log.info("launched %s", ids)


def launch_assemble_v13(root: Path, run: str, max_minutes: int, spot: bool = True) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    bundle = root / fleet.bundle_code(repo_root, root)
    ids = fleet.launch(
        f"{run}-assemble", [fleet.Assignment(0, [], 0.0, 0.0)], root / "plans_v13", bundle, 1, max_minutes,
        kind="assemble", command=f"assemble-v13 --run {run} --subset slice50 full", instance_types=fleet.INSTANCE_TYPES,
        volume_gb=300, upload_plans=False, spot=spot,
    )
    log.info("assembler %s", ids)


def assemble_v13(root: Path, run: str, subsets: list[str]) -> None:
    for subset in subsets:
        cube.add_v13(root, run, subset)
