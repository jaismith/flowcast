"""AWS Lambda entry point, invoked hourly by EventBridge Scheduler."""

from __future__ import annotations

import logging
import os

from .runner import run
from .store import Store

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    store = Store(os.environ["ARCHIVE_URI"])
    sources = (event or {}).get("sources")
    report = run(store, sources)
    summary = {"run_id": report.run_id, "new": report.new, "rows": report.rows, "files": len(report.files),
               "failed": report.failed, "seconds": report.seconds}
    logging.info("run summary %s", summary)
    if not report.ok:
        # Data that did arrive is already written and the state saved; failing the invocation
        # surfaces the partial failure on the Lambda Errors metric and alarm.
        raise RuntimeError(f"archiver run {report.run_id} had failures: {report.failed}")
    return summary
