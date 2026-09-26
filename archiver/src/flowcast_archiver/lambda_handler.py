"""AWS Lambda entry point, invoked hourly by EventBridge Scheduler.

Event (all optional): {"sources": [...], "force": true} to run a subset or ignore schedules.
"""

from __future__ import annotations

import logging
import os
import time

from .runner import run
from .store import Store

logging.getLogger().setLevel(logging.INFO)

# Leave room to write the last source's files and the state after the deadline check.
SHUTDOWN_MARGIN_S = 30


def handler(event, context):
    event = event or {}
    store = Store(os.environ["ARCHIVE_URI"])
    deadline = None
    if context is not None:
        deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - SHUTDOWN_MARGIN_S
    report = run(store, event.get("sources"), force=bool(event.get("force")), deadline=deadline)
    summary = {"run_id": report.run_id, "new": report.new, "rows": report.rows, "files": len(report.files),
               "failed": report.failed, "warnings": report.warnings, "skipped": report.skipped,
               "seconds": report.seconds}
    logging.info("run summary %s", summary)
    if not report.ok:
        # Data that did arrive is already written and the state saved; failing the invocation
        # surfaces the partial failure on the Lambda Errors metric and alarm.
        raise RuntimeError(f"archiver run {report.run_id} had failures: {report.failed}")
    return summary
