"""`flowcast-obs`: pull USGS observations into the `obs/` Parquet lake (a local directory or `s3://bucket/prefix`).

    flowcast-obs ingest  --out data --window-days 30   # every registry gauge (hourly job uses a 12 h window)
    flowcast-obs health  --out s3://bucket             # missed hourly cycles over the last 7 days
    flowcast-obs backfill --site 01427510 --start 1990-10-01 --out data
    flowcast-obs rating --site 01427510
"""

import argparse
import json
import logging
from dataclasses import asdict
from datetime import timedelta

import pandas as pd

from .ingest import VARIABLES, ingest, ingest_health
from .lake import Lake
from .obs import VARIABLE_PARAMETERS, to_hourly, write_obs
from .sites import ingest_gauges
from .usgs.client import WaterDataClient


def _pull(client: WaterDataClient, lake: Lake, site: str, variables: list[str], start, end) -> None:
    for variable in variables:
        iv = client.continuous(site, VARIABLE_PARAMETERS[variable], start, end, use_cache=True)
        hourly = to_hourly(iv)
        paths = write_obs(lake, site, variable, hourly)
        logging.info("%s %s: %d hourly rows -> %d files", site, variable, hourly["value"].notna().sum(), len(paths))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-obs")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_cmd = sub.add_parser("ingest", help="pull a recent window for every registry gauge")
    ingest_cmd.add_argument("--out", required=True)
    ingest_cmd.add_argument("--window-days", type=float, default=30)

    health = sub.add_parser("health", help="report missed hourly ingest cycles from the run markers")
    health.add_argument("--out", required=True)
    health.add_argument("--days", type=int, default=7)

    backfill = sub.add_parser("backfill", help="pull a historical range for one site (cached by year)")
    backfill.add_argument("--site", required=True)
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", default=None)
    backfill.add_argument("--variables", nargs="+", default=list(VARIABLES))
    backfill.add_argument("--out", required=True)

    rating = sub.add_parser("rating", help="print the current stage-discharge rating summary")
    rating.add_argument("--site", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = WaterDataClient()
    now = pd.Timestamp.now(tz="UTC")

    match args.command:
        case "ingest":
            report = ingest(Lake(args.out), ingest_gauges(), client, timedelta(days=args.window_days), now)
            print(json.dumps(asdict(report), indent=2))
        case "health":
            print(json.dumps(ingest_health(Lake(args.out), now, args.days), indent=2))
        case "backfill":
            _pull(client, Lake(args.out), args.site, args.variables, args.start, args.end or now)
        case "rating":
            curve = client.rating(args.site)
            print(f"{curve.site_id} rating {curve.rating_id} ({curve.kind}, retrieved {curve.retrieved})")
            print(f"stage {curve.stage_ft.min():.2f}-{curve.stage_ft.max():.2f} ft -> "
                  f"{curve.discharge_cfs.min():.0f}-{curve.discharge_cfs.max():.0f} ft3/s")
        case _:
            parser.error(f"unknown command {args.command}")
