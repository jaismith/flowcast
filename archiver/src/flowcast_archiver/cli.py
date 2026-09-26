from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from .runner import run
from .sources import SOURCES
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowcast-archiver", description="Archive NWS/NOAA benchmark forecasts.")
    parser.add_argument("--store", default=os.environ.get("ARCHIVE_URI", "lake"),
                        help="s3://bucket/prefix or a local directory (default: $ARCHIVE_URI or ./lake)")
    parser.add_argument("--sources", default=",".join(SOURCES), help=f"comma-separated subset of {','.join(SOURCES)}")
    parser.add_argument("--backfill-start", type=lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc),
                        help="where sources without a cursor start (default: each source's full history)")
    parser.add_argument("--until-caught-up", action="store_true",
                        help="repeat runs until the RVF backfill reaches the present (for a one-off local backfill)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    sources = [s for s in args.sources.split(",") if s]
    unknown = set(sources) - set(SOURCES)
    if unknown:
        parser.error(f"unknown sources: {', '.join(sorted(unknown))}")
    store = Store(args.store)
    ok = True
    while True:
        report = run(store, sources, backfill_start=args.backfill_start)
        print(json.dumps({"run_id": report.run_id, "new": report.new, "rows": report.rows,
                          "files": len(report.files), "failed": report.failed, "seconds": report.seconds}, indent=1))
        ok = ok and report.ok
        if not args.until_caught_up or report.new.get("marfc_rvf", 0) == 0:
            break
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
