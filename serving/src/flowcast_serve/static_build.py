"""`static.json` for a model basin (onboarding; runs on a laptop or agent VM, not in Lambda).

- `geometry`: NLDI basin outline; NHDPlus V2 flowlines upstream of the gauge with Strahler order (digitized
  upstream to downstream, as NHDPlus stores them); upstream NWIS gauges with their latest flow; NID dams in the
  basin. Ported from the landing prototype (PR #54, `prototypes/landing/scripts/build_data.py`).
- `watershed`: the smallest USGS WBD unit covering at least half the basin, and the units of that level with >= 5%
  (`prototypes/river-page/scripts/fetch_basin_name.py`).
- `flood_categories`: NWPS stages, converted to flow through the USGS EXSA rating.
- `climatology`: day-of-year quantiles of the local-date daily mean flow and water temperature (+-7 days), from the
  training cube's training years only (WY2001-2019); never WY2023+.
- `basin`: area, land cover, snow share, dams, the cube's maximum travel time, and median training-year flow.
"""

from __future__ import annotations

import io
import json
import logging
import time

import numpy as np
import pandas as pd
import requests
from flowcast_model.cube import Cube
from flowcast_pipeline.usgs.client import WaterDataClient
from shapely.geometry import Point, shape

from .registry import ServedSite

log = logging.getLogger(__name__)

NLDI = "https://api.water.usgs.gov/nldi/linked-data"
WFS = "https://api.water.usgs.gov/geoserver/wmadata/ows"
NID = "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/National_Inventory_of_Dams_Public_Service/FeatureServer/0/query"
WBD = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"
WBD_LEVELS = [(12, 6), (10, 5), (8, 4), (6, 3)]
TRAIN = (pd.Timestamp("2000-10-01"), pd.Timestamp("2019-09-30T23:00"))
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)
M3S_TO_CFS = 35.314666721
KM2_PER_MI2 = 2.589988110336
TZ = "America/New_York"

http = requests.Session()
http.headers["User-Agent"] = "flowcast-serving-onboarding (github.com/jaismith/flowcast)"


def get_json(url: str, **params):
    for attempt in range(5):
        try:
            r = http.get(url, params=params, timeout=180)
        except requests.RequestException:
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 2))
            continue
        r.raise_for_status()
    raise RuntimeError(f"{url} failed after retries")


def round_coords(coords, nd: int = 5):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [round_coords(c, nd) for c in coords]


def stream_order_min(area_km2: float) -> int:
    return 4 if area_km2 > 10000 else 3 if area_km2 > 2000 else 2 if area_km2 > 300 else 1


def geometry(site: str, area_km2: float, model_gauges: set[str], below_dam: set[str], usgs: WaterDataClient) -> tuple[dict, object]:
    info = get_json(f"{NLDI}/nwissite/USGS-{site}")
    comid = int(info["features"][0]["properties"]["comid"])
    basin = get_json(f"{NLDI}/nwissite/USGS-{site}/basin", simplified="true")
    poly = shape(basin["features"][0]["geometry"])
    x0, y0, x1, y1 = poly.bounds
    ut = get_json(f"{NLDI}/comid/{comid}/navigation/UT/flowlines", distance=2000)
    upstream = {int(f["properties"]["nhdplus_comid"]) for f in ut["features"]}
    cql = f"streamorde>={stream_order_min(area_km2)} AND BBOX(the_geom,{x0 - 0.01},{y0 - 0.01},{x1 + 0.01},{y1 + 0.01})"
    wfs = get_json(WFS, service="WFS", version="1.0.0", request="GetFeature", typeName="wmadata:nhdflowline_network", outputFormat="application/json",
                   propertyName="comid,gnis_name,streamorde,totdasqkm,the_geom", CQL_FILTER=cql)
    rivers = []
    for f in wfs["features"]:
        p = f["properties"]
        if p["comid"] not in upstream:
            continue
        lines = f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiLineString" else [f["geometry"]["coordinates"]]
        rivers.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": round_coords([c[:2] for c in sum(lines, [])])},
                       "properties": {"name": (p.get("gnis_name") or "").strip() or None, "order": p["streamorde"], "area_km2": round(p["totdasqkm"] or 0, 1)}})
    nwis = get_json(f"{NLDI}/nwissite/USGS-{site}/navigation/UT/nwissite", distance=2000)
    ids = [f["properties"]["identifier"].removeprefix("USGS-") for f in nwis["features"]]
    ids = [i for i in ids if i != site]
    frames = [usgs.latest_continuous(ids[k : k + 50], "00060") for k in range(0, len(ids), 50)]
    latest = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["monitoring_location_id", "time", "value"])
    latest = latest.set_index(latest["monitoring_location_id"].str.removeprefix("USGS-")) if len(latest) else latest
    now = pd.Timestamp.now(tz="UTC")
    gauges = []
    for f in nwis["features"]:
        sid = f["properties"]["identifier"].removeprefix("USGS-")
        if sid == site:
            continue
        active = sid in latest.index and (now - latest.loc[sid, "time"]) < pd.Timedelta(days=14)
        if not active and sid not in model_gauges:
            continue
        gauges.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": round_coords(f["geometry"]["coordinates"])},
                       "properties": {"id": f"USGS-{sid}", "name": f["properties"]["name"], "q": float(latest.loc[sid, "value"]) if active else None, "active": bool(active),
                                      "role": "below_dam" if sid in below_dam else ("model_input" if sid in model_gauges else "upstream")}})
    dams, offset = [], 0
    while True:
        page = get_json(NID, where="1=1", geometry=f"{x0},{y0},{x1},{y1}", geometryType="esriGeometryEnvelope", inSR=4326, outSR=4326, f="geojson",
                        outFields="NAME,NIDID,NID_STORAGE,NORMAL_STORAGE,PRIMARY_PURPOSE,YEAR_COMPLETED,NID_HEIGHT,RIVER_OR_STREAM", resultOffset=offset, resultRecordCount=1000)
        feats = page.get("features", [])
        for f in feats:
            xy = f["geometry"]["coordinates"]
            if not poly.contains(Point(xy)):
                continue
            p = f["properties"]
            dams.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": round_coords(xy)},
                         "properties": {"name": (p.get("NAME") or "").title(), "id": p.get("NIDID"), "storage_af": float(p.get("NID_STORAGE") or p.get("NORMAL_STORAGE") or 0),
                                        "purpose": p.get("PRIMARY_PURPOSE"), "year": p.get("YEAR_COMPLETED"), "height_ft": p.get("NID_HEIGHT"), "river": (p.get("RIVER_OR_STREAM") or "").title()}})
        if len(feats) < 1000:
            break
        offset += 1000
    dams.sort(key=lambda f: -f["properties"]["storage_af"])
    geom = basin["features"][0]["geometry"]
    doc = {
        "bounds": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
        "basin": {"type": "Feature", "geometry": {"type": geom["type"], "coordinates": round_coords(geom["coordinates"], 4)}, "properties": {}},
        "rivers": {"type": "FeatureCollection", "features": rivers},
        "gauges": {"type": "FeatureCollection", "features": gauges},
        "dams": {"type": "FeatureCollection", "features": dams},
        "sources": {"basin": "USGS NLDI (NHDPlus V2 catchments)", "rivers": "NHDPlus V2 flowlines upstream of the gauge (USGS NLDI + wmadata GeoServer)",
                    "gauges": "USGS NLDI upstream NWIS sites; active = discharge reported in the last 14 days", "dams": "USACE National Inventory of Dams"},
    }
    return doc, poly


def _inside(x: float, y: float, rings: list) -> bool:
    hit = False
    for ring in rings:
        for (xi, yi), (xj, yj) in zip(ring, ring[-1:] + ring[:-1], strict=True):
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
    return hit


def watershed(poly) -> dict | None:
    x0, y0, x1, y1 = poly.bounds
    n = 70
    pts = [(x0 + (i + 0.5) * (x1 - x0) / n, y0 + (j + 0.5) * (y1 - y0) / n) for i in range(n) for j in range(n)]
    pts = [p for p in pts if poly.contains(Point(p))]
    if not pts:
        return None
    for digits, layer in WBD_LEVELS:
        feats = get_json(WBD.format(layer=layer), geometry=f"{x0},{y0},{x1},{y1}", geometryType="esriGeometryEnvelope", inSR=4326, outSR=4326,
                         spatialRel="esriSpatialRelIntersects", outFields=f"huc{digits},name,areasqkm", returnGeometry="true", maxAllowableOffset=0.002, f="json").get("features", [])
        units = []
        for f in feats:
            share = sum(_inside(x, y, f["geometry"]["rings"]) for x, y in pts) / len(pts)
            if share > 0:
                a = f["attributes"]
                units.append({"huc": a[f"huc{digits}"], "name": a["name"], "share": round(share, 3)})
        units.sort(key=lambda u: -u["share"])
        if units and units[0]["share"] >= 0.5:
            return {"level": f"HUC-{digits}", "huc": units[0]["huc"], "name": units[0]["name"], "parts": [u for u in units if u["share"] >= 0.05],
                    "source": "USGS Watershed Boundary Dataset"}
    return None


def local_daily_mean(hourly: pd.Series) -> pd.Series:
    s = hourly.dropna()
    s.index = pd.DatetimeIndex(s.index).tz_localize("UTC")
    days = (s.index - pd.Timedelta(hours=1)).tz_convert(TZ).tz_localize(None).normalize()
    return s.groupby(days).mean()


def doy_climatology(daily: pd.Series, window: int = 7) -> list[list[float | None]]:
    d = daily.dropna()
    doy, vals = d.index.dayofyear.to_numpy(), d.to_numpy()
    out = []
    for k in range(1, 367):
        dist = np.minimum(np.abs(doy - k), 366 - np.abs(doy - k))
        v = vals[dist <= window]
        out.append([round(float(x), 2) for x in np.percentile(v, [100 * q for q in QUANTILES])] if len(v) >= 30 else [None] * len(QUANTILES))
    return out


def flood_categories(site: ServedSite | None, usgs_id: str, usgs: WaterDataClient, nwps: list[dict]) -> list[dict]:
    if not nwps:
        return []
    try:
        rating = usgs.rating(usgs_id)
        flows = rating.stage_to_discharge([c["stage_ft"] for c in nwps])
    except Exception as err:  # no EXSA rating published: keep the NWPS flows
        log.warning("%s: no USGS rating (%s); NWPS flows kept", usgs_id, err)
        return nwps
    return [{**c, "flow_cfs": round(float(f), 0) if np.isfinite(f) else c.get("flow_cfs"), "flow_source": "USGS EXSA rating" if np.isfinite(f) else "NWPS"} for c, f in zip(nwps, flows, strict=True)]


def build(cube: Cube, site: ServedSite, entry: dict, statics: dict, travel: pd.DataFrame, outflows: list[str], model_gauges: set[str], nwps: list[dict], usgs: WaterDataClient) -> dict:
    b = site.usgs_id
    geo, poly = geometry(b, float(statics["area_km2"]), model_gauges | set(outflows), set(outflows), usgs)
    hourly = cube.load_dynamic(b, ["qobs_m3s", "tw_c"], *TRAIN)
    q_daily = local_daily_mean(hourly["qobs_m3s"] * M3S_TO_CFS)
    tw_daily = local_daily_mean(hourly["tw_c"])
    n_major = statics.get("nid_n_major")
    n_major = int(n_major) if n_major is not None and np.isfinite(n_major) else sum(1 for d in geo["dams"]["features"] if d["properties"]["storage_af"] >= 5000)
    return {
        "schema": "flowcast.static/v1", "id": site.site_id, "slug": site.slug, "name": entry.get("name", site.name), "short_name": site.short_name,
        "river": entry.get("river"), "lat": site.lat, "lon": site.lon, "timezone": site.timezone, "has_temp": bool(entry.get("has_temp")),
        "nws_lid": site.nws_lid, "usgs_url": f"https://waterdata.usgs.gov/monitoring-location/{site.site_id}/",
        "basin": {
            "area_km2": round(float(statics["area_km2"]), 1), "area_sq_mi": entry.get("area_mi2"), "elevation_m": statics.get("elevation_m"),
            "forest_frac": statics.get("forest_frac"), "developed_frac": statics.get("developed_frac"), "frac_snow": statics.get("frac_snow"),
            "below_dam": bool(statics.get("below_dam", 0) > 0), "n_major_dams": n_major, "n_dams": len(geo["dams"]["features"]),
            "travel_time_max_h": round(float(travel.loc[b, "tt_max_h"]), 1) if b in travel.index and np.isfinite(travel.loc[b, "tt_max_h"]) else None,
            "median_flow_cfs": round(float(q_daily.median()), 1) if q_daily.notna().any() else None,
        },
        "watershed": watershed(poly),
        "flood_categories": flood_categories(site, b, usgs, nwps),
        "climatology": {"quantiles": list(QUANTILES), "years": "WY2001-2019 (training years)", "flow_cfs": doy_climatology(q_daily),
                        "water_temp_c": doy_climatology(tw_daily) if tw_daily.notna().sum() >= 365 else None},
        "geometry": geo,
        "watershed_description": None,
    }


TRAVEL_KEY = "sites/traveltime_summary.parquet"


class StaticBatch:
    """Builds `sites/USGS-{id}/serving/static.json` for model basins with the production versions' statics; one
    shared, throttled USGS client (the key is shared with production)."""

    def __init__(self, lake, registry, cube: str, entries: dict[str, dict], sites: dict[str, ServedSite], min_interval_s: float = 9.0):
        pointer = registry.production()
        flow_root, _ = registry.fetch("flow", pointer["flow"])
        temp_root = registry.fetch("temp", pointer["temp"])[0] if pointer.get("temp") else None
        self.lake, self.entries, self.sites = lake, entries, sites
        self.statics = pd.read_parquet(flow_root / "statics.parquet")
        self.outflows = json.loads((flow_root / "outflows.json").read_text())
        self.temp_gauges = json.loads((temp_root / "temp_gauges.json").read_text()) if temp_root else {}
        self.travel = pd.read_parquet(io.BytesIO(lake.read(TRAVEL_KEY)))
        self.cube = Cube([cube])
        self.usgs = WaterDataClient(min_interval_s=min_interval_s)

    @staticmethod
    def key(basin: str) -> str:
        return f"sites/USGS-{basin}/serving/static.json"

    def pending(self, basins: list[str] | None = None) -> list[str]:
        basins = basins or list(self.statics.index)
        return [b for b in basins if self.lake.read(self.key(b)) is None]

    def build_one(self, b: str) -> str | None:
        """Builds and stores one basin's static.json; returns an error text instead of raising."""
        try:
            info = json.loads(self.lake.read(f"sites/USGS-{b}/serving/site.json") or b"{}")
            tg = self.temp_gauges.get(b, {})
            outflows = self.outflows.get(b, [])
            model_gauges = set(outflows) | ({tg["upstream"]} if tg.get("upstream") else set()) | set(tg.get("outflow", []))
            doc = build(self.cube, self.sites[f"USGS-{b}"], self.entries.get(f"USGS-{b}", {}), self.statics.loc[b].to_dict(), self.travel, outflows,
                        model_gauges, info.get("flood_categories", []), self.usgs)
            self.lake.write(self.key(b), json.dumps(doc, separators=(",", ":")).encode(), "application/json")
            return None
        except Exception as err:  # one basin's web service failure must not stop the batch
            return repr(err)[:300]
