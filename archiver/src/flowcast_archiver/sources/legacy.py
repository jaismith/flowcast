"""The current flowcast site's own forecast (the "old model" baseline) until it's retired."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pandas as pd

from ..context import Context
from ..model import Issuance

log = logging.getLogger(__name__)

URL = "https://api.flowcast.jaismith.dev/forecast"
SITE = "01427510"
_VARIABLES = {"streamflow": "flow_cfs", "watertemp": "water_temp_f"}


def normalize(body: dict, fetched_at) -> pd.DataFrame:
    fc = body["forecast"]
    issue_time = pd.Timestamp(fc["origin_timestamp"], unit="s", tz="UTC")
    frames = []
    for name, variable in _VARIABLES.items():
        block = (fc.get("water_forecast") or {}).get(name)
        if not block:
            continue
        valid = pd.to_datetime(block["timestamps"], unit="s", utc=True)
        series = [(None, block["values"])]
        ci = block.get("confidence_intervals") or {}
        series += [(0.05, ci["5th"])] if "5th" in ci else []
        series += [(0.95, ci["95th"])] if "95th" in ci else []
        for quantile, values in series:
            frames.append(pd.DataFrame({"variable": variable, "valid_time": valid, "quantile": quantile, "value": values}))
    df = pd.concat(frames, ignore_index=True)
    df["dataset"] = "flowcast_legacy"
    df["location_id"] = SITE
    df["usgs_site"] = SITE
    df["issue_time"] = issue_time
    df["fetched_at"] = fetched_at
    return df


def collect(ctx: Context) -> Iterator[Issuance]:
    resp, raw = ctx.get(URL, params={"usgs_site": SITE})
    body = resp.json()
    origin = body.get("forecast", {}).get("origin_timestamp")
    if origin is None:
        return
    key = f"{SITE}/{origin}"
    if ctx.state.has("flowcast_legacy", key):
        return
    frame = normalize(body, raw.fetched_at)
    yield Issuance("flowcast_legacy", key, pd.Timestamp(origin, unit="s", tz="UTC").to_pydatetime(), frame, [raw])
