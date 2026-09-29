"""Lower Colorado River Authority (TX) Highland Lakes: observed hourly dam discharge and flood-operation notices.

The Hydromet site's own JavaScript calls these JSON endpoints (no key):
* `api/turbinedata/GetHourlyTotalDischargePast2Weeks`: hourly total discharge (cfs) for Buchanan, Inks, Wirtz (LBJ),
  Starcke (Marble Falls), Mansfield (Travis) and Tom Miller (Austin), last 14 days.
* `api/FloodStatus/GetLakeLevelsGateOps`: per dam, head/tail elevations and free-text inflow, gate-operation and
  forecast statements ("No gate operations to pass floodwaters are expected").
LCRA publishes no hourly release schedule outside floods; the gate-ops text is the forward-looking part.
"""

from __future__ import annotations

from collections.abc import Iterator

import pandas as pd

from ..fetch import Fetched, Fetcher
from ..schema import Kind, frame

BASE = "https://hydromet.lcra.org/"
DISCHARGE = BASE + "api/turbinedata/GetHourlyTotalDischargePast2Weeks"
GATE_OPS = BASE + "api/FloodStatus/GetLakeLevelsGateOps"

DAMS = {
    "buchananDischarge": "Buchanan",
    "inksDischarge": "Inks",
    "lbjDischarge": "Wirtz",
    "marbleFallsDischarge": "Starcke",
    "travisDischarge": "Mansfield",
    "austinDischarge": "Tom Miller",
}


def parse_discharge(body: dict, fetched: Fetched) -> pd.DataFrame:
    recs = body.get("records") or []
    if not recs:
        raise ValueError("LCRA discharge: no records")
    missing = set(DAMS) - set(recs[0])
    if missing:
        raise ValueError(f"LCRA discharge: fields missing {sorted(missing)}")
    rows = []
    for r in recs:
        # The timestamp is the end of the hour the total covers.
        end = pd.Timestamp(r["dateTime"])
        for field, dam in DAMS.items():
            rows.append({"source": "lcra", "dam": dam, "kind": Kind.OBSERVED_RELEASE, "valid_start": end - pd.Timedelta(hours=1),
                         "valid_end": end, "value": r.get(field), "unit": "cfs", "issue_time": end, "fetched_at": fetched.fetched_at,
                         "note": None})
    return frame(rows)


def parse_gate_ops(body: dict, fetched: Fetched) -> pd.DataFrame:
    rows = []
    for r in body.get("records") or []:
        issued = pd.Timestamp(r.get("lastUpdate") or fetched.fetched_at)
        text = " ".join(filter(None, (r.get("inflows"), r.get("gateOps"))))
        rows.append({"source": "lcra", "dam": r["dam"], "kind": Kind.NOTICE, "valid_start": issued, "valid_end": issued,
                     "value": None, "unit": None, "issue_time": issued, "fetched_at": fetched.fetched_at, "note": text})
        if r.get("head") is not None:
            when = pd.Timestamp(r.get("lastDataUpdate") or fetched.fetched_at)
            rows.append({"source": "lcra", "dam": r["dam"], "kind": Kind.POOL_ELEVATION, "valid_start": when, "valid_end": when,
                         "value": r["head"], "unit": "ft", "issue_time": when, "fetched_at": fetched.fetched_at, "note": "head"})
    return frame(rows)


def collect(fetcher: Fetcher) -> Iterator[tuple[pd.DataFrame, dict]]:
    got = fetcher.get(DISCHARGE)
    yield parse_discharge(got.json(), got), {"url": got.url}
    got = fetcher.get(GATE_OPS)
    yield parse_gate_ops(got.json(), got), {"url": got.url}
