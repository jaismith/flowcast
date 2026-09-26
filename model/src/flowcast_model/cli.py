"""`flowcast-model` command line.

    flowcast-model train --config configs/handoff_cmal.yml --cube data/cube.zarr --run-dir runs/x [--set hidden_size=128]
    flowcast-model hindcast --run-dir runs/x --out runs/x/hindcast
    flowcast-model score --forecasts runs/*/hindcast --cube data/cube.zarr --out results/sweep
    flowcast-model prepare-public --out data/public-smoke.zarr
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path

import yaml

from .config import apply_overrides, prepare_run, read_raw


def parse_sets(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        key, _, value = item.partition("=")
        out[key] = yaml.safe_load(value)
    return out


def s3_sync(local: Path, remote: str) -> None:
    subprocess.run(["aws", "s3", "sync", str(local), remote, "--only-show-errors", "--exclude", "*.tmp"], check=True, timeout=1800)


def generic_inputs(cfg) -> tuple[list[str], list[str]]:
    derived = {f"{f}_shift{s}" for f, shifts in cfg.lagged_features.items() for s in (shifts if isinstance(shifts, list) else [shifts])}
    features = sorted((set(cfg.dynamic_inputs_flattened) | set(cfg.target_variables) | set(cfg.lagged_features)) - derived)
    return features, list(cfg.static_attributes)


def cmd_train(args) -> None:
    from .cube import Cube, CubeDims
    from .dataset import ZarrCubeDataset
    from .stock import export_generic
    from .trainer import train

    raw = apply_overrides(read_raw(args.config), parse_sets(args.set))
    cfg, options = prepare_run(raw, args.run_dir, cube_paths=args.cube)
    ZarrCubeDataset.configure(options.dataset)
    if cfg.dataset == "generic":
        generic_dir = Path(args.run_dir) / "generic"
        if not (generic_dir / "attributes" / "attributes.csv").exists():
            features, attributes = generic_inputs(cfg)
            basins = (Path(args.run_dir) / "basins.txt").read_text().split()
            cube = Cube(options.dataset.cube, CubeDims.from_dict(options.dataset.dims))
            export_generic(cube, generic_dir, basins, features, attributes, "2000-10-01", "2022-09-30T23:00")
        cfg.update_config({"data_dir": str(generic_dir)})
    on_checkpoint = (lambda epoch: s3_sync(Path(args.run_dir), args.sync_to)) if args.sync_to else None
    train(cfg, on_checkpoint=on_checkpoint)


def cmd_hindcast(args) -> None:
    from .config import load_run
    from .hindcast import hindcast

    if not load_run(args.run_dir)[1].hindcast.enabled:
        logging.info("hindcast disabled in the run config")
        return

    hindcast(args.run_dir, args.out or Path(args.run_dir) / "hindcast", period=args.period, epoch=args.epoch, n_samples=args.n_samples, extra_issues=args.extra_issues, basins=args.basins, cube_paths=args.cube)


def cmd_score(args) -> None:
    from .score import score_runs

    summary = score_runs(args.forecasts, args.cube, args.out, target=args.target, unit=args.unit, area_attribute=args.area_attribute, nwm_attribute=args.nwm_attribute or None, n_boot=args.n_boot, workers=args.workers)
    print((Path(args.out) / "summary.md").read_text())
    del summary


def cmd_score_run(args) -> None:
    from .config import load_run
    from .score import score_runs

    run_dir = Path(args.run_dir)
    _, options = load_run(run_dir)
    sopts = options.score
    if not sopts.enabled:
        logging.info("scoring disabled in the run config")
        return
    score_runs(
        [run_dir / "hindcast"],
        options.dataset.cube,
        run_dir / "scores",
        target=sopts.target,
        unit=options.target.get("unit", "mm/h"),
        area_attribute=options.target.get("area_attribute"),
        nwm_attribute=sopts.nwm_attribute,
        dims=options.dataset.dims,
        n_boot=sopts.n_boot,
        workers=sopts.workers,
    )


def cmd_prepare_public(args) -> None:
    from .publicdata import build_public_cube

    build_public_cube(args.out, basins=args.basins)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-model")
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("train", help="train (or resume) one run in a fixed run directory")
    t.add_argument("--config", required=True)
    t.add_argument("--run-dir", required=True)
    t.add_argument("--cube", nargs="+", default=None, help="cube store(s); overrides flowcast.dataset.cube")
    t.add_argument("--set", nargs="*", default=[], help="dotted overrides, e.g. hidden_size=128 flowcast.dataset.block_basins=8")
    t.add_argument("--sync-to", default=None, help="s3:// prefix to sync the run directory to after every checkpoint")

    h = sub.add_parser("hindcast", help="issue-time hindcasts in the evaluation interchange format")
    h.add_argument("--run-dir", required=True)
    h.add_argument("--out", default=None)
    h.add_argument("--period", default="validation", choices=["validation", "train", "test"])
    h.add_argument("--epoch", default=None)
    h.add_argument("--n-samples", type=int, default=None)
    h.add_argument("--basins", nargs="*", default=None)
    h.add_argument("--cube", nargs="+", default=None, help="cube store(s), if not where the run was trained")
    h.add_argument("--extra-issues", default=None, help="Parquet (site_id, issue_time) of extra issue times, e.g. MARFC bulletins")

    s = sub.add_parser("score", help="score hindcasts of one or more runs against baselines (validation years)")
    s.add_argument("--forecasts", nargs="+", required=True)
    s.add_argument("--cube", nargs="+", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--target", default="qobs_mm_h")
    s.add_argument("--unit", default="mm/h")
    s.add_argument("--area-attribute", default="area_km2")
    s.add_argument("--nwm-attribute", default="nwm_feature_id")
    s.add_argument("--n-boot", type=int, default=1000)
    s.add_argument("--workers", type=int, default=1)

    sr = sub.add_parser("score-run", help="score a run's own hindcasts if its config enables it (used by the Spot job)")
    sr.add_argument("--run-dir", required=True)

    p = sub.add_parser("prepare-public", help="build the small public smoke-test cube (WY2001-2022)")
    p.add_argument("--out", required=True)
    p.add_argument("--basins", nargs="*", default=None)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    {"train": cmd_train, "hindcast": cmd_hindcast, "score": cmd_score, "score-run": cmd_score_run, "prepare-public": cmd_prepare_public}[args.command](args)
