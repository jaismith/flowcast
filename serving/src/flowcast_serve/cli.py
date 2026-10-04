"""`flowcast-serve`: promote models, onboard sites, run forecasts and parity checks from a laptop or agent VM.

Uses the environment the Lambdas use (LAKE_URI, DATA_BUCKET, CONTROL_TABLE, FORECAST_FUNCTION); see serving/README.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.snow.params import NORTHEAST

from . import (
    bundles,
    config,
    cycle,
    eligibility,
    forecast,
    light,
    onboard,
    parity,
    promote,
    site_index,
    snodas_live,
    static_build,
)
from .control import Control
from .issues import utcnow
from .registry import (
    FAMILIES,
    INDEX_KEY,
    ModelRegistry,
    registry_overrides,
    resolve,
    served_sites,
)


def _registry(settings: config.Settings) -> ModelRegistry:
    return ModelRegistry(settings.lake_uri.removeprefix("s3://").split("/", 1)[0])


def build_statics(s: config.Settings, basins: list[str] | None, cube: str, workers: int, skip_existing: bool, min_interval: float = 0.0) -> dict:
    lake = Lake(s.lake_uri)
    batch = static_build.StaticBatch(lake, _registry(s), cube, {e["id"]: e for e in light.index_entries(lake)}, served_sites(), min_interval)
    todo = batch.pending(basins) if skip_existing else (basins or list(batch.statics.index))
    with ThreadPoolExecutor(workers) as pool:
        errors = dict(zip(todo, pool.map(batch.build_one, todo), strict=True))
    return {"built": sum(e is None for e in errors.values()), "failed": {b: e for b, e in errors.items() if e}}


def build_eligibility(s: config.Settings, out_dir: Path | None) -> dict:
    now = pd.Timestamp(utcnow())
    table = eligibility.build({x.usgs_id for x in served_sites().values()}, now)
    index, tiles = eligibility.documents(table, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if out_dir:
        (out_dir / "tiles").mkdir(parents=True, exist_ok=True)
        (out_dir / "index.json").write_text(json.dumps(index, indent=1))
        for k, t in tiles.items():
            (out_dir / "tiles" / f"{k}.json").write_text(json.dumps(t, separators=(",", ":")))
        table.to_parquet(out_dir / "eligibility.parquet")
    else:
        light.publish_gauges(bundles.DataBucket(s.data_bucket, s.data_prefix), index, tiles)
    return index["counts"]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="flowcast-serve")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("promote", help="build a registry version from training runs and upload it")
    pr.add_argument("family", choices=FAMILIES)
    pr.add_argument("--version", required=True)
    pr.add_argument("--runs", nargs="*", default=[], help="s3://…/runs/<run> per seed")
    pr.add_argument("--cube", help="the runs' training cube (statics source)")
    pr.add_argument("--calibration", type=Path)
    pr.add_argument("--outflows", type=Path, help="work/meta/outflows.json of the cube build")
    pr.add_argument("--temp-source-cube", help="full trainval cube for the temperature gauge choice")
    pr.add_argument("--notes", default="")
    pr.add_argument("--out", type=Path, default=Path("/tmp/flowcast/promote"))
    pr.add_argument("--activate", action="store_true")

    ac = sub.add_parser("activate", help="point production at versions (rollback = activate older ones)")
    for f in FAMILIES:
        ac.add_argument(f"--{f}")
    ac.add_argument("--note", default="")
    sub.add_parser("history", help="list every production pointer written")

    ob = sub.add_parser("onboard", help="per-basin weights, HRUs and NWPS categories")
    ob.add_argument("--basins", nargs="*", help="USGS numbers (default: every basin of the production flow version)")
    ob.add_argument("--plans", type=Path, required=True)
    ob.add_argument("--meta", type=Path, required=True)
    ob.add_argument("--hrus", type=Path, required=True, help="local HRU cache (load_or_build_hrus)")
    ob.add_argument("--build-hrus", action="store_true")

    bs = sub.add_parser("build-static", help="static.json per basin (geometry, watershed, climatology, flood flows) into the lake")
    bs.add_argument("--basins", nargs="*", help="USGS numbers (default: every basin of the production flow version)")
    bs.add_argument("--cube", required=True, help="the flow model's training cube (training years are read for climatology)")
    bs.add_argument("--workers", type=int, default=4)
    bs.add_argument("--skip-existing", action="store_true")
    bs.add_argument("--min-interval", type=float, default=0.0, help="seconds between USGS API requests (the key is shared with production)")

    el = sub.add_parser("build-eligibility", help="evaluate the site-eligibility rule for every lower-48 discharge gauge; publish /data/v1/gauges/")
    el.add_argument("--out", type=Path, help="write here instead of the data bucket")

    ix = sub.add_parser("build-index", help="write sites/index.json from the cube's basin selection")
    ix.add_argument("--selection", type=Path, required=True)

    rn = sub.add_parser("run", help="forecast sites for an issue in this process")
    rn.add_argument("--sites", nargs="+", required=True)
    rn.add_argument("--issue", help="YYYYMMDDHH (default: the latest available)")
    rn.add_argument("--trigger", default="manual")

    sub.add_parser("cycle", help="the scheduled cycle (invokes the forecast function)")
    sub.add_parser("light", help="the hourly light build")
    sn = sub.add_parser("snodas", help="daily SNODAS ingest for every servable basin")
    sn.add_argument("--days-back", type=int, default=35)

    al = sub.add_parser("alerts", help="alerts hook: add (+1) or remove (-1) an alert subscription for a site")
    al.add_argument("--site", required=True)
    al.add_argument("--delta", type=int, choices=(1, -1), required=True)

    pe = sub.add_parser("parity-extract", help="gate 2: live extraction vs the training cube over a validation window")
    pe.add_argument("--basin", required=True)
    pe.add_argument("--start", required=True)
    pe.add_argument("--end", required=True)
    pe.add_argument("--cube", required=True)
    pe.add_argument("--plans", type=Path, required=True)
    pm = sub.add_parser("parity-model", help="gate 1: live predictor on cube inputs vs the stored hindcast mixtures")
    pm.add_argument("--basin", required=True)
    pm.add_argument("--issue", required=True, help="a validation issue, e.g. 2021-06-15T12:00")
    pm.add_argument("--cube", required=True)
    pm.add_argument("--family", choices=("flow", "temp"), default="flow")
    pm.add_argument("--hindcast-mixture", required=True, help="the seed's hindcast_mixture dir (s3:// or local)")

    a = p.parse_args(argv)
    s = config.Settings()
    if a.cmd == "promote":
        root = promote.snow_version(a.version, NORTHEAST, a.out, a.notes) if a.family == "snow" else promote.build_version(
            a.family, a.version, a.runs, a.cube, a.out, a.calibration, a.outflows, a.temp_source_cube, a.notes)
        reg = _registry(s)
        promote.upload(reg, root, a.family, a.version)
        out = {"uploaded": f"models/{a.family}/{a.version}"}
        if a.activate:
            current = reg.read_json("models/production.json") or {}
            out["pointer"] = reg.set_production({**{f: current[f] for f in FAMILIES if current.get(f)}, a.family: a.version}, note=f"promote {a.family} {a.version}")
    elif a.cmd == "activate":
        reg = _registry(s)
        current = reg.read_json("models/production.json") or {}
        out = reg.set_production({f: getattr(a, f) or current.get(f) for f in FAMILIES if getattr(a, f) or current.get(f)}, note=a.note)
    elif a.cmd == "history":
        out = _registry(s).history()
    elif a.cmd == "onboard":
        reg = _registry(s)
        flow_root, _ = reg.fetch("flow", reg.production()["flow"])
        basins = a.basins or list(pd.read_parquet(flow_root / "statics.parquet").index)
        out = onboard.onboard(Lake(s.lake_uri), basins, a.plans, a.meta, a.hrus, registry_overrides(), a.build_hrus)
    elif a.cmd == "build-static":
        out = build_statics(s, a.basins, a.cube, a.workers, a.skip_existing, a.min_interval)
    elif a.cmd == "build-eligibility":
        out = build_eligibility(s, a.out)
    elif a.cmd == "build-index":
        reg = _registry(s)
        pointer = reg.production()
        flow_root, _ = reg.fetch("flow", pointer["flow"])
        temp_root = reg.fetch("temp", pointer["temp"])[0] if pointer.get("temp") else None
        flow = list(pd.read_parquet(flow_root / "statics.parquet").index)
        temp = list(pd.read_parquet(temp_root / "statics.parquet").index) if temp_root else []
        entries = site_index.build(pd.read_parquet(a.selection), flow, temp, registry_overrides())
        Lake(s.lake_uri).write(INDEX_KEY, site_index.to_bytes(entries), "application/json")
        out = {"sites": len(entries), "with_temperature": sum(e["has_temp"] for e in entries)}
    elif a.cmd == "run":
        ids = [resolve(x).site_id for x in a.sites]
        issue = a.issue or cycle.target_issue(utcnow())
        out = forecast.run(ids, issue, a.trigger, False, s)
    elif a.cmd == "cycle":
        out = cycle.run(s)
    elif a.cmd == "light":
        out = light.run(s)
    elif a.cmd == "snodas":
        out = snodas_live.ingest(Lake(s.lake_uri), [x.usgs_id for x in served_sites().values()], pd.Timestamp(utcnow()), a.days_back)
    elif a.cmd == "alerts":
        site = resolve(a.site)
        out = {"site": site.site_id, "alerts": Control(s.table).set_alerts(site.site_id, a.delta)}
    elif a.cmd == "parity-extract":
        out = parity.extraction(a.basin, pd.Timestamp(a.start, tz="UTC"), pd.Timestamp(a.end, tz="UTC"), a.cube, a.plans)
    elif a.cmd == "parity-model":
        out = parity.model(_registry(s), a.family, a.basin, pd.Timestamp(a.issue, tz="UTC"), a.cube, a.hindcast_mixture)
    json.dump(out, sys.stdout, indent=1, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
