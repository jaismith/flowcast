"""TVA predicted reservoir operations (the backend of the TVA Lake Info app).

`predicted-data/{site}.json` gives daily average inflow and outflow (cfs) and the
midnight pool elevation for today plus two days, updated at least daily by 1 p.m. ET.
The endpoint is undocumented and live-only; a new version is archived whenever its
content changes, issued at retrieval time. Sites are NWS location ids (see
`tva_sites` in forecast_points.yaml).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pandas as pd

from ..context import Context
from ..model import Issuance
from .common import EASTERN, digest, local_midnight_utc, number

URL = "https://www.tva.com/RestApi/predicted-data/{site}.json"


def normalize(body: list[dict], site: str, fetched_at) -> pd.DataFrame:
    rows = []
    for day in body:
        d = datetime.strptime(day["Day"], "%m/%d/%Y").date()
        start = local_midnight_utc(d, EASTERN)
        end = local_midnight_utc(d + timedelta(days=1), EASTERN)
        for field, variable, when, qualifier in (
            ("AverageInflow", "inflow_cfs", start, "daily_mean"),
            ("AverageOutflow", "outflow_cfs", start, "daily_mean"),
            ("MidnightElevation", "pool_elev_ft", end, "midnight"),
        ):
            value = number(day.get(field))
            if value is not None:
                rows.append((variable, when, value, qualifier))
    df = pd.DataFrame(rows, columns=["variable", "valid_time", "value", "qualifier"])
    df["dataset"] = "tva_predicted"
    df["location_id"] = site
    df["issue_time"] = fetched_at
    df["fetched_at"] = fetched_at
    return df


def collect(ctx: Context) -> Iterator[Issuance]:
    empty = 0
    for site in ctx.config.tva_sites:
        try:
            resp, raw = ctx.get(URL.format(site=site))
            body = resp.json()
        except Exception as exc:
            ctx.warn("TVA %s failed: %s", site, exc)
            continue
        if not body:
            empty += 1
            continue
        key = f"{site}/{digest(raw.body)}"
        if ctx.state.has("tva_predicted", key):
            continue
        yield Issuance("tva_predicted", key, raw.fetched_at, normalize(body, site, raw.fetched_at), [raw])
    if ctx.config.tva_sites and empty == len(ctx.config.tva_sites):
        ctx.fail("TVA returned no predictions for any site; the endpoint may have changed")
