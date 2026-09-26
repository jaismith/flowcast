"""Experimental HEFS API: 65-member MEFP-forced ensembles, daily, 6-hourly to 30 days.

The API keeps roughly the last 10 issues per point, so each run archives every
issue it lists that hasn't been archived yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pandas as pd

from ..context import Context
from ..model import Issuance

log = logging.getLogger(__name__)

API = "https://api.water.noaa.gov/hefs/v1"

# HEFS parameter -> archive variable
VARIABLES = {
    "QINE": "flow_cfs",  # instantaneous flow
    "FMAP": "precip_in",  # 6-h basin-mean precipitation accumulation
    "FMAT": "air_temp_f",  # 6-h basin-mean air temperature
    "SWE": "swe_in",  # modelled snow water equivalent
}
UNITS = {"CFS": 1.0, "IN": 1.0, "DEGF": 1.0}


def normalize_ensembles(groups: list[list[dict]], usgs: str | None, fetched_at) -> pd.DataFrame:
    frames = []
    for group in groups:
        for m in group:
            events = m.get("events") or []
            if not events:
                continue
            df = pd.DataFrame(events).dropna(subset=["value"])
            if m.get("miss_val") is not None:
                df = df[df["value"] != m["miss_val"]]
            param = m["parameter_id"]
            qualifier = ",".join(m.get("qualifier_id") or []) or None
            frames.append(
                pd.DataFrame(
                    {
                        "valid_time": pd.to_datetime(df["valid_datetime"], utc=True),
                        "value": df["value"].astype("float64") * UNITS.get(m.get("units"), 1.0),
                        "variable": VARIABLES.get(param, param.lower()),
                        "member": m["ensemble_member_index"],
                        "qualifier": qualifier,
                        "issue_time": pd.Timestamp(m["forecast_datetime"]),
                        "location_id": m["location_id"],
                    }
                )
            )
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["dataset"] = "hefs"
    df["usgs_site"] = usgs
    df["fetched_at"] = fetched_at
    return df


def collect(ctx: Context) -> Iterator[Issuance]:
    for point in ctx.config.points:
        if not point.hefs:
            continue
        try:
            resp, _ = ctx.get(f"{API}/headers/", params={"location_id": point.lid, "parameter_id": "QINE", "limit": 100})
        except Exception:
            ctx.fail("HEFS headers failed for %s", point.lid)
            continue
        issues = sorted({h["forecast_datetime"] for h in resp.json()})
        for issue in issues:
            key = f"{point.lid}/{issue}"
            if ctx.state.has("hefs", key):
                continue
            try:
                resp, raw = ctx.get(
                    f"{API}/ensembles/", params={"location_id": point.lid, "forecast_datetime": issue}, timeout=120
                )
            except Exception:
                ctx.fail("HEFS ensembles failed for %s %s", point.lid, issue)
                continue
            frame = normalize_ensembles(resp.json(), point.usgs, raw.fetched_at)
            if frame.empty:
                continue
            yield Issuance("hefs", key, pd.Timestamp(issue).to_pydatetime(), frame, [raw])
