"""`flowcast-eval` command line.

    flowcast-eval fetch-nwm --site 01427510              # fill the NWM caches (retrospective + operational archive)
    flowcast-eval scoreboard --site 01427510 --out results/USGS-01427510
    flowcast-eval score --forecasts archive/ --site 01427510 --variable discharge --out results/archive
    flowcast-eval strong-baselines --site 01427510 --lake s3://<lake> --archive s3://<archive>/baselines --out results/USGS-01427510/strong_baselines
    flowcast-eval skill-page --site 01427510 --lake s3://<lake> --archive s3://<archive>/baselines [--web s3://<web>]
"""

import argparse
import logging

import pandas as pd

from flowcast_pipeline.lake import Lake
from flowcast_pipeline.sites import get_site

from . import nwm
from .protocol import FROZEN_TEST, HOURLY_LEADS_H, NWM_OPERATIONAL, VALIDATION
from .scoreboard import score_archived_forecasts, site_scoreboard
from .skillpage import Config, run
from .strong import score_strong, write_results

NWM_PRODUCTS = ["medium_range_mem1", "medium_range_blend", "short_range", *[f"medium_range_mem{k}" for k in range(2, 7)]]


def fetch_nwm(site_id: str, start: str, end: str | None, products: list[str], workers: int) -> None:
    site = get_site(site_id)
    nwm.retrospective(site.nwm_reach, start="2000-10-01", end="2023-02-01")
    logging.info("retrospective cached for reach %s", site.nwm_reach)
    end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC").floor("D") - pd.Timedelta(days=1)
    for product in products:
        hours = "6h" if product == "short_range" else "D"
        cycles = pd.date_range(pd.Timestamp(start, tz="UTC"), end_ts, freq=hours)
        ensemble_only = product.startswith("medium_range_mem") and product != "medium_range_mem1"
        leads = nwm.ENSEMBLE_LEADS_H if ensemble_only else HOURLY_LEADS_H
        df = nwm.operational_forecasts(site.nwm_reach, site.id, product, cycles, leads, workers=workers)
        logging.info("%s: %d values over %d cycles", product, len(df), df["issue_time"].nunique())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-eval")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch-nwm", help="cache NWM retrospective and archived operational forecasts for a site")
    f.add_argument("--site", required=True)
    f.add_argument("--start", default=NWM_OPERATIONAL.test_start)
    f.add_argument("--end", default=None)
    f.add_argument("--products", nargs="+", default=NWM_PRODUCTS)
    f.add_argument("--workers", type=int, default=32)

    s = sub.add_parser("scoreboard", help="run all reference baselines (and cached NWM) for a site")
    s.add_argument("--site", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--n-boot", type=int, default=FROZEN_TEST.n_boot)
    s.add_argument("--skip-nwm", action="store_true")
    s.add_argument("--skip-temperature", action="store_true")

    a = sub.add_parser("score", help="score archived forecasts (interchange format) against baselines at the same issue times")
    a.add_argument("--forecasts", nargs="+", required=True)
    a.add_argument("--site", required=True)
    a.add_argument("--variable", default="discharge")
    a.add_argument("--out", required=True)
    a.add_argument("--period", choices=["frozen-test", "validation"], default="frozen-test", help="fitting years and allowed issue window")
    a.add_argument("--obs", default=None, help="Parquet with `time` (UTC) and `value` (ft3/s) to verify against instead of USGS")
    a.add_argument("--nwm-reach", type=int, default=None, help="add the NWM v3.0 retrospective for this reach as a reference")

    k = sub.add_parser("skill-page", help="nightly job: score baselines, NWM and the forecast archive; publish the skill page")
    k.add_argument("--site", default="USGS-01427510")
    k.add_argument("--lake", required=True, help="obs lake URI (s3://bucket or a local directory)")
    k.add_argument("--archive", required=True, help="forecast archive URI, e.g. s3://flowcast-archiver-<account>-<region>/baselines")
    k.add_argument("--web", default=None, help="web bucket URI; omit to only write metrics to the lake")
    k.add_argument("--n-boot", type=int, default=FROZEN_TEST.n_boot)
    k.add_argument("--skip-nwm", action="store_true")

    b = sub.add_parser("strong-baselines", help="fit routing/ARX/LightGBM baselines on the training years and score them on validation years")
    b.add_argument("--site", default="USGS-01427510")
    b.add_argument("--lake", required=True)
    b.add_argument("--archive", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--n-boot", type=int, default=FROZEN_TEST.n_boot)
    b.add_argument("--no-publish", action="store_true", help="don't write the skill-page payload to the lake")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    match args.command:
        case "fetch-nwm":
            fetch_nwm(args.site, args.start, args.end, args.products, args.workers)
        case "scoreboard":
            site_scoreboard(args.site, args.out, n_boot=args.n_boot, include_nwm=not args.skip_nwm, include_temperature=not args.skip_temperature)
        case "score":
            obs = None
            if args.obs:
                frame = pd.read_parquet(args.obs)
                obs = frame.set_index(pd.to_datetime(frame["time"], utc=True))["value"]
            protocol = VALIDATION if args.period == "validation" else FROZEN_TEST
            score_archived_forecasts(args.forecasts, args.site, args.variable, args.out, obs=obs, protocol=protocol, nwm_reach=args.nwm_reach)
        case "strong-baselines":
            lake = Lake(args.lake)
            result = score_strong(lake, Lake(args.archive), args.site, args.n_boot)
            write_results(result, args.out, None if args.no_publish else lake, get_site(args.site).id)
        case "skill-page":
            run(Config(site_id=args.site, lake_uri=args.lake, archive_uri=args.archive, web_uri=args.web, n_boot=args.n_boot, include_nwm=not args.skip_nwm))
        case _:
            parser.error(f"unknown command {args.command}")
