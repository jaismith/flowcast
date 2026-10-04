"""The site index behind `/data/v1/sites.json`: every basin the production flow model covers.

Built once per model promotion (`flowcast-serve build-index`) from the cube's basin selection (GAGES-II station
names, gauge coordinates, NWIS drainage areas) and the models' basin lists, and stored at `sites/index.json` in the
lake. The hourly light build adds each site's readiness and newest forecast (`bundles.sites_document`).
"""

from __future__ import annotations

import json

import pandas as pd

from .names import parse
from .registry import ServedSite

KM2_PER_MI2 = 2.589988110336


def build(selection: pd.DataFrame, flow_basins: list[str], temp_basins: list[str], served: dict[str, ServedSite]) -> list[dict]:
    by_usgs = {s.usgs_id: s for s in served.values()}
    temp = set(temp_basins)
    out = []
    for b in flow_basins:
        row = selection.loc[b]
        n = parse(str(row["STANAME"]), str(row["STATE"]))
        area_km2 = row.get("NWIS_DRAIN_SQKM")
        if pd.isna(area_km2) or not area_km2:
            area_km2 = row["DRAIN_SQKM"]
        site = by_usgs.get(b)
        out.append({
            "id": f"USGS-{b}",
            "slug": site.slug if site else None,
            "name": site.name if site else n.name,
            "river": n.river,
            "town": n.town,
            "state": n.state,
            "lat": round(float(site.lat if site else row["LAT_GAGE"]), 6),
            "lon": round(float(site.lon if site else row["LNG_GAGE"]), 6),
            "area_mi2": round(float(area_km2) / KM2_PER_MI2, 1),
            "has_temp": b in temp,
            "in_training_region": True,
        })
    return sorted(out, key=lambda e: (e["state"], e["river"], e["town"]))


def to_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"sites": entries}, indent=0).encode()
