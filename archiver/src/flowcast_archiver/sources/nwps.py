"""NWPS JSON API: MARFC deterministic forecasts per gauge and NWM series per reach.

Both endpoints only ever return the latest issuance, so anything not polled
while current is lost (NWM can later be recovered from noaa-nwm-pds; MARFC
from RVF bulletins, see iem.py).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pandas as pd

from ..context import Context
from ..model import Issuance

log = logging.getLogger(__name__)

API = "https://api.water.noaa.gov/nwps/v1"

# NWPS series name -> (dataset, key in the response body)
NWM_SERIES = {
    "short_range": ("nwm_short_range", "shortRange"),
    "medium_range": ("nwm_medium_range", "mediumRange"),
    "medium_range_blend": ("nwm_medium_range_blend", "mediumRangeBlend"),
    "analysis_assimilation": ("nwm_analysis_assim", "analysisAssimilation"),
    "long_range": ("nwm_long_range", "longRange"),
}

_UNIT_SCALE = {"kcfs": ("flow_cfs", 1000.0), "cfs": ("flow_cfs", 1.0), "ft": (None, 1.0)}


def _primary_variable(kind: str) -> str:
    return "pool_elev_ft" if kind == "pool" else "stage_ft"


def normalize_stageflow_forecast(body: dict, lid: str, usgs: str | None, kind: str, fetched_at) -> pd.DataFrame:
    issue_time = pd.Timestamp(body["issuedTime"])
    rows = []
    secondary_unit = (body.get("secondaryUnits") or "").lower()
    for point in body.get("data", []):
        valid = pd.Timestamp(point["validTime"])
        primary = point.get("primary")
        if primary is not None and primary > -999:
            rows.append((_primary_variable(kind), valid, primary))
        secondary = point.get("secondary")
        if secondary is not None and secondary > -999 and secondary_unit in _UNIT_SCALE:
            var, scale = _UNIT_SCALE[secondary_unit]
            if var:
                rows.append((var, valid, secondary * scale))
    df = pd.DataFrame(rows, columns=["variable", "valid_time", "value"])
    df["dataset"] = "marfc_nwps"
    df["location_id"] = lid
    df["usgs_site"] = usgs
    df["issue_time"] = issue_time
    df["qualifier"] = body.get("pedts")
    df["fetched_at"] = fetched_at
    return df


def collect_marfc(ctx: Context) -> Iterator[Issuance]:
    for point in ctx.config.points:
        try:
            resp, raw = ctx.get(f"{API}/gauges/{point.lid}/stageflow/forecast")
        except Exception:
            ctx.fail("NWPS stageflow failed for %s", point.lid)
            continue
        body = resp.json()
        if not body.get("issuedTime") or not body.get("data"):
            continue
        issue_time = pd.Timestamp(body["issuedTime"]).to_pydatetime()
        key = f"{point.lid}/{body['issuedTime']}"
        if ctx.state.has("marfc_nwps", key):
            continue
        frame = normalize_stageflow_forecast(body, point.lid, point.usgs, point.kind, raw.fetched_at)
        yield Issuance("marfc_nwps", key, issue_time, frame, [raw])


def normalize_reach_series(body: dict, series: str, reach: str, usgs: str | None, fetched_at) -> list[tuple[str, pd.Timestamp, pd.DataFrame]]:
    """Returns (dataset, reference_time, frame) for the series in a reach payload."""
    dataset, body_key = NWM_SERIES[series]
    block = body.get(body_key) or {}
    out = []
    frames = []
    reference_time = None
    for name, s in block.items():
        if not isinstance(s, dict) or not s.get("data"):
            continue
        reference_time = pd.Timestamp(s["referenceTime"])
        member = int(name.removeprefix("member")) if name.startswith("member") else None
        df = pd.DataFrame(s["data"]).rename(columns={"validTime": "valid_time", "flow": "value"})
        df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
        df["member"] = member
        df["qualifier"] = "mean" if name == "mean" else None
        df["issue_time"] = reference_time
        frames.append(df)
    if frames:
        df = pd.concat(frames, ignore_index=True)
        df["dataset"] = dataset
        df["location_id"] = reach
        df["usgs_site"] = usgs
        df["variable"] = "flow_cfs"
        df["fetched_at"] = fetched_at
        out.append((dataset, reference_time, df))
    return out


def collect_nwm(ctx: Context) -> Iterator[Issuance]:
    for reach, point in ctx.config.reaches.items():
        for series in NWM_SERIES:
            try:
                resp, raw = ctx.get(f"{API}/reaches/{reach}/streamflow", params={"series": series})
            except Exception:
                ctx.fail("NWPS reach %s %s failed", reach, series)
                continue
            for dataset, reference_time, frame in normalize_reach_series(resp.json(), series, reach, point.usgs, raw.fetched_at):
                key = f"{reach}/{reference_time.isoformat()}"
                if ctx.state.has(dataset, key):
                    continue
                yield Issuance(dataset, key, reference_time.to_pydatetime(), frame, [raw])
