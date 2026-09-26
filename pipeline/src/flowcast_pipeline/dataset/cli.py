"""`flowcast-dataset`: build training cube v1.

    flowcast-dataset prepare          # CAMELSH + NID + USGS inventory -> basins, regulation, slice, terrain, weights, plans
    flowcast-dataset targets          # USGS hourly discharge / water temperature (rate-limited, resumable)
    flowcast-dataset launch --run r1 --instances 4 --max-minutes 240
    flowcast-dataset status --run r1
    flowcast-dataset extract-worker --job job.json --plans plans/ --out out/   # on the Spot instance
    flowcast-dataset assemble --run r1 --subset slice50|full
"""

import argparse
import logging
from pathlib import Path

from . import build, fleet
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
        case _:
            parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    main()
