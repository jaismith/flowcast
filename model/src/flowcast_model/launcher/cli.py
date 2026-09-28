"""`flowcast-train`: EC2 Spot training runs.

    flowcast-train setup
    flowcast-train upload-dataset --src data/public-smoke.zarr --name public-smoke
    flowcast-train launch --config configs/handoff_cmal_public.yml --dataset s3://.../cube.zarr --max-hours 2
    flowcast-train launch --sweep configs/sweeps/smoke.yml          # one Spot instance per variant, in parallel
    flowcast-train launch --config ... --run-id <id> --instance-type g5.xlarge --on-demand   # resume a run On-Demand
    flowcast-train status [--prefix smoke]
    flowcast-train cost [--prefix smoke]
    flowcast-train fetch --run-id <id> --dest runs/
    flowcast-train kill --run-id <id> | --all

Sweep file:

    name: smoke
    config: configs/handoff_cmal_public.yml
    datasets: [s3://bucket/datasets/x/cube.zarr]
    instance_type: g5.2xlarge
    max_hours: 2.5
    runs:                 # variant name -> dotted overrides of the base config
      base: {}
      hidden256: {hidden_size: 256, hindcast_hidden_size: 256, forecast_hidden_size: 256}
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import apply_overrides, read_raw
from . import aws

REPO = Path(__file__).resolve().parents[4]


def _run_id(*parts: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    rid = "-".join([*parts, stamp]).lower()
    return re.sub(r"[^a-z0-9-]+", "-", rid)[:56]


def _launch_with_retry(args, launch):
    give_up = time.time() + 60 * args.retry_minutes
    while True:
        try:
            return launch()
        except aws.NoCapacityError as err:
            if time.time() >= give_up:
                raise
            logging.warning("%s; retrying in 5 min", err)
            time.sleep(300)


def cmd_launch(args, acct: aws.Account) -> None:
    if args.sweep:
        sweep = yaml.safe_load(Path(args.sweep).read_text())
        base = read_raw(REPO / "model" / sweep["config"] if not Path(sweep["config"]).is_absolute() else sweep["config"])
        name = sweep["name"]
        existing = {r["run_id"] for r in aws.list_runs(acct, f"{name}-")} if args.reuse_runs else set()

        def run_id_for(key: str) -> str:
            prefix = _run_id(name, key).rsplit("-", 2)[0] + "-"
            matches = sorted(r for r in existing if r.startswith(prefix) and aws.has_checkpoint(acct, r))
            return matches[-1] if matches else _run_id(name, key)

        runs = [aws.RunSpec(run_id_for(key), apply_overrides(base, ov), ov or {}) for key, ov in sweep["runs"].items() if not args.only or key in args.only]
        datasets = args.dataset or sweep["datasets"]
        itype = args.instance_type or sweep.get("instance_type", "g5.2xlarge")
        hours = args.max_hours or sweep.get("max_hours", 3.0)
    else:
        base = read_raw(args.config)
        overrides = {k: yaml.safe_load(v) for k, _, v in (s.partition("=") for s in args.set)}
        name = args.name or base.get("experiment_name", "run")
        runs = [aws.RunSpec(args.run_id or _run_id(name), apply_overrides(base, overrides), overrides)]
        datasets, itype, hours, sweep = args.dataset, args.instance_type or "g5.2xlarge", args.max_hours or 3.0, None
        if not datasets:
            raise SystemExit("--dataset is required without --sweep")
    sweep_opts = sweep if isinstance(sweep, dict) else {}
    region = args.region or sweep_opts.get("region")
    if args.on_demand and itype == "auto":
        raise SystemExit("--on-demand needs an explicit --instance-type (auto placement plans Spot quota)")
    if itype == "auto":
        gpu_types = tuple(sweep_opts.get("gpu_instance_types", aws.GPU_PREFERENCE))
        cpu_type = sweep_opts.get("cpu_instance_type", args.cpu_instance_type)
        slots = aws.plan_gpu_slots(acct, len(runs), gpu_types)
        overflow = sweep_opts.get("cpu_overflow", args.cpu_overflow)
        groups: dict[tuple[str, str], list[aws.RunSpec]] = {}
        for i, r in enumerate(runs):
            if i < len(slots):
                key = slots[i]
            elif overflow:
                key = (cpu_type, acct.home_region)
            else:
                print(f"queued (no free GPU slot): {r.run_id}")
                continue
            r.config = apply_overrides(r.config, sweep_opts.get("gpu_overrides" if aws.is_gpu(key[0]) else "cpu_overrides", {}))
            groups.setdefault(key, []).append(r)
        if args.dry_run:
            for (t, reg), rs in groups.items():
                for r in rs:
                    print(reg, t, r.run_id, json.dumps(r.overrides))
            return
        for (t, reg), rs in groups.items():
            launched = _launch_with_retry(args, lambda: aws.launch(acct.in_region(reg), rs, datasets, REPO, instance_type=t, max_hours=hours, max_price=args.max_price, sweep=name if args.sweep else None, replicate=not args.no_replicate and sweep_opts.get("replicate_dataset", True), on_demand=args.on_demand, data_on_ebs=args.data_on_ebs, avoid_azs=tuple(args.avoid_az)))
            for m in launched:
                print(f"{m['run_id']}  {t}  {m['instance_id']}  {m['availability_zone']}  deadline {m['deadline']}")
        return
    if region == "auto":
        region = aws.pick_region(acct, itype, len(runs))
    if region:
        acct = acct.in_region(region)
    replicate = not args.no_replicate and sweep_opts.get("replicate_dataset", True)
    if args.dry_run:
        for r in runs:
            print(acct.region, r.run_id, json.dumps(r.overrides))
        return
    launched = _launch_with_retry(args, lambda: aws.launch(acct, runs, datasets, REPO, instance_type=itype, max_hours=hours, max_price=args.max_price, sweep=name if args.sweep else None, replicate=replicate, on_demand=args.on_demand, data_on_ebs=args.data_on_ebs, avoid_azs=tuple(args.avoid_az)))
    for m in launched:
        print(f"{m['run_id']}  {m['instance_id']}  {m['availability_zone']}  deadline {m['deadline']}  datasets {' '.join(m['datasets'])}")


def cmd_status(args, acct: aws.Account) -> None:
    for r in aws.list_runs(acct, args.prefix):
        s = r.get("status") or {}
        print(f"{r['run_id']:<56} {r.get('instance_state', '?'):<10} {s.get('status', '-'):<10} {s.get('detail', '')} ({s.get('time', '')})")


def cmd_cost(args, acct: aws.Account) -> None:
    total = 0.0
    for r in aws.list_runs(acct, args.prefix):
        c = aws.run_cost(acct, r["run_id"])
        total += c["total_usd"]
        print(json.dumps(c))
    print(f"TOTAL ${total:.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-train")
    sub = parser.add_subparsers(dest="command", required=True)
    su = sub.add_parser("setup", help="create/update the shared training resources (idempotent)")
    su.add_argument("--region", default=None)
    q = sub.add_parser("quotas", help="G/VT Spot quota and Spot price per candidate region")
    q.add_argument("--instance-type", default="g5.2xlarge")

    l = sub.add_parser("launch", help="launch one run, or a sweep of runs in parallel, on EC2 Spot")  # noqa: E741
    l.add_argument("--config")
    l.add_argument("--sweep")
    l.add_argument("--name")
    l.add_argument("--run-id", default=None, help="relaunch an existing run: it resumes from its latest checkpoint in S3")
    l.add_argument("--set", nargs="*", default=[])
    l.add_argument("--dataset", nargs="+", default=None, help="s3:// URI(s) of the cube store(s)")
    l.add_argument("--instance-type", default=None, help="EC2 type, or 'auto': GPU if a region has G/VT Spot quota, else --cpu-instance-type")
    l.add_argument("--cpu-instance-type", default="c8g.4xlarge")
    l.add_argument("--cpu-overflow", action="store_true", help="with --instance-type auto, runs beyond the free GPU slots go to CPU Spot instead of waiting")
    l.add_argument("--max-hours", type=float, default=None, help="hard max runtime per instance")
    l.add_argument("--max-price", type=float, default=None, help="max Spot price in USD/h")
    l.add_argument("--region", default=None, help="compute region, or 'auto' for the cheapest region whose G/VT Spot quota fits the runs")
    l.add_argument("--on-demand", action="store_true", help="On-Demand instead of Spot (opt-in; needs On-Demand G/VT quota and an explicit --instance-type)")
    l.add_argument("--avoid-az", nargs="*", default=[], help="availability zones not to launch in (e.g. a sibling run's zone)")
    l.add_argument("--data-on-ebs", action="store_true", help="keep the datasets on the persistent root volume instead of instance NVMe, so a Spot stop/start skips the dataset copy")
    l.add_argument("--no-replicate", action="store_true", help="read a dataset in another region directly instead of from its replica in the compute region")
    l.add_argument("--retry-minutes", type=float, default=0, help="keep retrying for Spot capacity/quota this long")
    l.add_argument("--only", nargs="*", default=None, help="sweep variants to launch (default: all)")
    l.add_argument("--reuse-runs", action="store_true", help="relaunch each variant's latest existing run (resumes from its checkpoints; a finished run only re-hindcasts and scores)")
    l.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("--prefix", default="")
    c = sub.add_parser("cost")
    c.add_argument("--prefix", default="")
    f = sub.add_parser("fetch")
    f.add_argument("--run-id", required=True)
    f.add_argument("--dest", default="runs")
    f.add_argument("--checkpoints", action="store_true")
    k = sub.add_parser("kill")
    k.add_argument("--run-id", nargs="*", default=None)
    k.add_argument("--all", action="store_true")
    u = sub.add_parser("upload-dataset")
    u.add_argument("--src", required=True)
    u.add_argument("--name", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    acct = aws.Account()
    match args.command:
        case "setup":
            print(json.dumps(aws.setup(acct.in_region(args.region) if args.region else acct), indent=2))
        case "quotas":
            try:
                print("pick:", aws.pick_region(acct, args.instance_type, 1))
            except RuntimeError as err:
                print(err)
        case "launch":
            cmd_launch(args, acct)
        case "status":
            cmd_status(args, acct)
        case "cost":
            cmd_cost(args, acct)
        case "fetch":
            print(aws.fetch(acct, args.run_id, Path(args.dest), args.checkpoints))
        case "kill":
            if not args.all and not args.run_id:
                raise SystemExit("pass --run-id or --all")
            print(aws.kill(acct, None if args.all else args.run_id))
        case "upload-dataset":
            print(aws.upload_dataset(acct, Path(args.src), args.name))
        case _:
            parser.error(args.command)
