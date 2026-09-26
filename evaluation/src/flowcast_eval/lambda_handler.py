"""AWS Lambda entry point for the nightly skill-page job (EventBridge Scheduler)."""

from __future__ import annotations

import logging

from .skillpage import Config, run

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    payload = run(Config.from_env())
    summary = {"generated": payload["generated"], "sections": len(payload["sections"]), "seconds": payload["seconds"]}
    logging.info("skill page summary %s", summary)
    return summary
