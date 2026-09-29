"""Find the gauge that measures a dam's release, from the dam's coordinates alone.

NLDI snaps the point to its NHDPlus flowline and navigates downstream on the main stem; the nearest
USGS sites with continuous discharge are candidates. This is the same linkage onboarding needs for
any dam in the NID, not just the curated prototype list.
"""

from __future__ import annotations

import math

import pandas as pd
import requests
from flowcast_pipeline.usgs import Parameter, WaterDataClient

NLDI = "https://api.water.usgs.gov/nldi/linked-data"


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def downstream_sites(lat: float, lon: float, distance_km: float = 30.0, session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    pos = s.get(f"{NLDI}/comid/position", params={"coords": f"POINT({lon} {lat})"}, timeout=60)
    pos.raise_for_status()
    comid = pos.json()["features"][0]["properties"]["comid"]
    nav = s.get(f"{NLDI}/comid/{comid}/navigation/DM/nwissite", params={"distance": distance_km}, timeout=60)
    nav.raise_for_status()
    out = []
    for f in nav.json().get("features", []):
        ident = f["properties"]["identifier"]
        if not ident.startswith("USGS-") or len(ident) > 15:  # 15-digit ids are lake, well and point sites
            continue
        glon, glat = f["geometry"]["coordinates"][:2]
        out.append({"site": ident.removeprefix("USGS-"), "name": f["properties"]["name"], "km": _km(lat, lon, glat, glon), "comid": comid})
    return sorted(out, key=lambda r: r["km"])


def active_discharge(site: str, client: WaterDataClient, within_days: int = 30) -> bool:
    """True if the site has an instantaneous discharge series with data in the last `within_days`.

    Many gauges named "below ... Dam" are discontinued (e.g. Bull Shoals, Summersville since 2003),
    so the name alone isn't enough.
    """
    meta = client.time_series_metadata(site, Parameter.DISCHARGE)
    if meta.empty or "computation_period_identifier" not in meta:
        return False
    points = meta[meta["computation_period_identifier"] == "Points"]
    if points.empty:
        return False
    end = pd.to_datetime(points["end_utc"], utc=True).max()
    return end >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=within_days)


def below_dam_gauge(lat: float, lon: float, client: WaterDataClient, distance_km: float = 30.0) -> dict | None:
    """Nearest downstream site with active continuous discharge."""
    for cand in downstream_sites(lat, lon, distance_km):
        if active_discharge(cand["site"], client):
            return cand
    return None
