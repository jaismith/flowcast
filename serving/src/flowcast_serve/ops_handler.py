"""Entry point of the `flowcast-serve-ops` zip function: `{"action": "cycle"}` from the four daily schedules and
`{"action": "light"}` hourly (infra-v2/lib/serving.ts)."""

from __future__ import annotations

import logging

from . import cycle, light

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    action = (event or {}).get("action")
    if action == "cycle":
        return cycle.run(issue=event.get("issue"))
    if action == "light":
        return light.run()
    raise ValueError(f"unknown ops action {action!r}")
