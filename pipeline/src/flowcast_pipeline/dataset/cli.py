"""`flowcast-dataset`: build training cube v1.

    flowcast-dataset prepare          # CAMELSH + NID + USGS inventory -> basins, regulation, slice, terrain, weights, plans
    flowcast-dataset targets          # USGS hourly discharge / water temperature (rate-limited, resumable)
    flowcast-dataset launch --run r1 --instances 4 --max-minutes 240
    flowcast-dataset status --run r1
    flowcast-dataset extract-worker --job job.json --plans plans/ --out out/   # on the Spot instance
    flowcast-dataset assemble --run r1 --subset slice50|full
    flowcast-dataset bench --store s3://.../v1/full/trainval.zarr
    flowcast-dataset prepare-reforecast && flowcast-dataset launch-reforecast --run rf1   # v1.1 GEFSv12 reforecast
    flowcast-dataset launch-assemble-v11 --run rf1
    flowcast-dataset discover-upstream && flowcast-dataset upstream-targets && flowcast-dataset assemble-v12   # v1.2
"""

import argparse
import logging
from pathlib import Path

from . import build, fleet, reader
from .config import work_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("targets")
    launch = sub.add_parser("launch")
    launch.add_argument("--run", required=True)
    launch.add_argument("--instances", type=int, default=4)
    launch.add_argument("--workers", type=int, default=16, help="processes per instance (I/O bound, so > vCPUs)")
    launch.add_argument("--max-minutes", type=int, default=240)
    launch.add_argument("--dry-run", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--run", required=True)
    worker = sub.add_parser("extract-worker")
    worker.add_argument("--job", type=Path, required=True)
    worker.add_argument("--plans", type=Path, required=True)
    worker.add_argument("--out", type=Path, required=True)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("--run", required=True)
    assemble.add_argument("--subset", choices=["slice50", "full"], required=True)
    assemble.add_argument("--upload", action="store_true")
    sub.add_parser("prepare-reforecast", help="plans for the GEFSv12 reforecast and operational GEFS elevation bands")
    lrf = sub.add_parser("launch-reforecast")
    lrf.add_argument("--run", required=True)
    lrf.add_argument("--rf-instances", type=int, default=10)
    lrf.add_argument("--band-instances", type=int, default=3)
    lrf.add_argument("--workers", type=int, default=12, help="decode processes per reforecast instance")
    lrf.add_argument("--max-minutes", type=int, default=180)
    lasm = sub.add_parser("launch-assemble-v11")
    lasm.add_argument("--run", required=True)
    lasm.add_argument("--max-minutes", type=int, default=240)
    lasm.add_argument("--on-demand", action="store_true", help="use On-Demand when other jobs hold the shared Spot quota")
    av11 = sub.add_parser("assemble-v11", help="append v1.1 arrays to copies of the v1 stores in S3")
    av11.add_argument("--run", required=True)
    av11.add_argument("--subset", nargs="+", default=["slice50", "full"])
    sub.add_parser("discover-upstream", help="NLDI upstream-gauge discovery for every basin")
    sub.add_parser("upstream-targets", help="discharge for outermost upstream gauges (rate-limited)")
    av12 = sub.add_parser("assemble-v12", help="append upstream-gauge features to copies of the v1.1 stores in S3")
    av12.add_argument("--subset", nargs="+", default=["slice50", "full"])
    sub.add_parser("prepare-zones", help="travel-time zones and v1.3 plans")
    lz = sub.add_parser("launch-zones")
    lz.add_argument("--run", required=True)
    lz.add_argument("--aorc-instances", type=int, default=4)
    lz.add_argument("--rf-instances", type=int, default=8)
    lz.add_argument("--max-minutes", type=int, default=180)
    lz13 = sub.add_parser("launch-assemble-v13")
    lz13.add_argument("--run", required=True)
    lz13.add_argument("--max-minutes", type=int, default=180)
    lz13.add_argument("--on-demand", action="store_true")
    av13 = sub.add_parser("assemble-v13")
    av13.add_argument("--run", required=True)
    av13.add_argument("--subset", nargs="+", default=["slice50", "full"])
    sub.add_parser("prepare-rt-zones", help="travel-time zones on the MRMS and HRRR grids, and their plans")
    lrt = sub.add_parser("launch-rt-zones")
    lrt.add_argument("--run", required=True)
    lrt.add_argument("--instances", type=int, default=4)
    lrt.add_argument("--max-minutes", type=int, default=150)
    lart = sub.add_parser("launch-assemble-rt-zones")
    lart.add_argument("--run", required=True)
    lart.add_argument("--max-minutes", type=int, default=150)
    lart.add_argument("--on-demand", action="store_true")
    art = sub.add_parser("assemble-rt-zones", help="append MRMS/HRRR zone arrays to the v1.3 stores in place")
    art.add_argument("--run", required=True)
    art.add_argument("--subset", nargs="+", default=["slice50", "full"])
    sub.add_parser("refresh-regulation", help="recompute regulation.parquet and outflows.json for the existing selection")
    arf = sub.add_parser("assemble-regulation-fix", help="rewrite regulation statics and gauged outflow in the v1.3 stores in place; add constant-flow arrays")
    arf.add_argument("--subset", nargs="+", default=["slice50", "full"])
    bench = sub.add_parser("bench", help="time loading basin blocks of all hourly variables")
    bench.add_argument("--store", required=True, help="s3://... or local path to a .zarr store")
    bench.add_argument("--k", type=int, default=16)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("botocore", "urllib3", "s3transfer", "flowcast_pipeline.usgs.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = work_dir()

    match args.command:
        case "prepare":
            build.prepare(root)
        case "targets":
            build.pull_targets(root)
        case "launch":
            build.launch_extraction(root, args.run, args.instances, args.workers, args.max_minutes, args.dry_run)
        case "status":
            build.status(args.run)
        case "extract-worker":
            fleet.run_worker(args.job, args.plans, args.out)
        case "assemble":
            build.assemble(root, args.run, args.subset, args.upload)
        case "prepare-reforecast":
            build.prepare_reforecast(root)
        case "launch-reforecast":
            build.launch_reforecast(root, args.run, args.rf_instances, args.band_instances, args.workers, args.max_minutes)
        case "launch-assemble-v11":
            build.launch_assemble_v11(root, args.run, args.max_minutes, spot=not args.on_demand)
        case "assemble-v11":
            build.assemble_v11(root, args.run, args.subset)
        case "discover-upstream":
            build.discover_upstream(root)
        case "upstream-targets":
            build.pull_upstream_targets(root)
        case "assemble-v12":
            build.assemble_v12(root, args.subset)
        case "prepare-zones":
            build.prepare_zones(root)
        case "launch-zones":
            build.launch_zones(root, args.run, args.aorc_instances, args.rf_instances, args.max_minutes)
        case "launch-assemble-v13":
            build.launch_assemble_v13(root, args.run, args.max_minutes, spot=not args.on_demand)
        case "assemble-v13":
            build.assemble_v13(root, args.run, args.subset)
        case "prepare-rt-zones":
            build.prepare_rt_zones(root)
        case "launch-rt-zones":
            build.launch_rt_zones(root, args.run, args.instances, args.max_minutes)
        case "launch-assemble-rt-zones":
            build.launch_assemble_rt(root, args.run, args.max_minutes, spot=not args.on_demand)
        case "assemble-rt-zones":
            build.assemble_rt_zones(root, args.run, args.subset)
        case "refresh-regulation":
            build.refresh_regulation(root)
        case "assemble-regulation-fix":
            build.assemble_regulation_fix(root, args.subset)
        case "bench":
            print(reader.benchmark(args.store, args.k))
        case _:
            parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    main()
