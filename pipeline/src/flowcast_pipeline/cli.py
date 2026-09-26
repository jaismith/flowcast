"""`flowcast-obs`: pull USGS observations into the local `obs/` Parquet lake.

    flowcast-obs ingest  --out data            # last 30 days for every registry site (hourly + revisions)
    flowcast-obs backfill --site 01427510 --start 1990-10-01 --out data
    flowcast-obs rating --site 01427510
"""

import argparse
import logging
from datetime import timedelta

import pandas as pd

from .obs import VARIABLE_PARAMETERS, to_hourly, write_obs
from .sites import load_sites
from .usgs.client import WaterDataClient


def _pull(client: WaterDataClient, out: str, site: str, variables: list[str], start, end, use_cache: bool) -> None:
    for variable in variables:
        iv = client.continuous(site, VARIABLE_PARAMETERS[variable], start, end, use_cache=use_cache)
        hourly = to_hourly(iv)
        paths = write_obs(out, site, variable, hourly)
        logging.info("%s %s: %d hourly rows -> %d files", site, variable, hourly["value"].notna().sum(), len(paths))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-obs")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="re-pull a recent window for all registry sites")
    ingest.add_argument("--out", required=True)
    ingest.add_argument("--window-days", type=int, default=30)

    backfill = sub.add_parser("backfill", help="pull a historical range for one site (cached by year)")
    backfill.add_argument("--site", required=True)
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", default=None)
    backfill.add_argument("--variables", nargs="+", default=["discharge", "water_temperature"])
    backfill.add_argument("--out", required=True)

    rating = sub.add_parser("rating", help="print the current stage-discharge rating summary")
    rating.add_argument("--site", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = WaterDataClient()
    now = pd.Timestamp.now(tz="UTC")

    match args.command:
        case "ingest":
            for site in load_sites().values():
                _pull(client, args.out, site.id, list(site.variables), now - timedelta(days=args.window_days), now, use_cache=False)
        case "backfill":
            _pull(client, args.out, args.site, args.variables, args.start, args.end or now, use_cache=True)
        case "rating":
            curve = client.rating(args.site)
            print(f"{curve.site_id} rating {curve.rating_id} ({curve.kind}, retrieved {curve.retrieved})")
            print(f"stage {curve.stage_ft.min():.2f}-{curve.stage_ft.max():.2f} ft -> "
                  f"{curve.discharge_cfs.min():.0f}-{curve.discharge_cfs.max():.0f} ft3/s")
        case _:
            parser.error(f"unknown command {args.command}")
