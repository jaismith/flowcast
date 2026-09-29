"""v1.4: storage and observed releases of the reservoirs upstream of each basin (rebuild plan §4.1).

Regulation signals that don't depend on operator schedules, from structured feeds that have both real-time data and
history (see docs/dam-schedule-scraping.md, "Recommendation" step 1).

**Dams.** NID main structures with at least 1,000 acre-ft of capacity (normal storage, or maximum storage for dry
flood-control dams), matched to basins exactly as the v1.3 regulation attributes are (`regulation.dams_in_basins`).

**Per dam, a fill series and a release series,** each merged from sources in order of preference. A source only
counts if it still reports (a real-time feed); archives without one (ResOpsUS) only backfill years before a live
source of the same kind starts, so every signal seen in training is also available operationally.

* Fill, storage-based: CWMS observed storage (all USACE offices) -> NYC DEP usable storage (Cannonsville, Pepacton,
  Neversink) -> USGS reservoir storage (00054) -> ResOpsUS daily storage. Fill = storage / NID capacity.
* Fill, pool-based (dams with no storage feed): CWMS pool elevation -> USGS lake/reservoir elevation. Fill is a
  relative pool index: 0 at the 5th and 1 at the 95th percentile of the dam's record before the frozen test years.
* Release: CWMS observed outflow -> an active USGS discharge gauge on the main stem at most 15 km below the dam
  (NLDI) -> ResOpsUS daily outflow.

USBR RISE, TVA and CDEC are checked too (`other_feeds`): none has a station at a basin dam, since the basins lie in
HUC2 01/02/04/05.

**Timing.** Sub-daily sources give hour-ending means at *t* (lag them by an hour in the model, like `qobs_mm_h`).
A daily value for local day D is used for every hour of day D+1 (18-48 h old), so it was published by then.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import xarray as xr
import zarr

from ..usgs.cache import ResponseCache
from ..usgs.client import WaterDataClient
from ..usgs.params import Parameter
from . import camelsh, config, cube, regulation, targets, traveltime

log = logging.getLogger(__name__)

MIN_CAPACITY_AF = 1_000.0
ACRE_FT_M3 = 1233.48184
CFS_M3S = 0.028316846592
EQUAL_AREA = "EPSG:5070"
TIMEZONE = "America/New_York"
ACTIVE_SINCE = pd.Timestamp("2026-08-01", tz="UTC")  # CWMS catalog extents lag the data by about two weeks
POOL_PCT = (5.0, 95.0)

CWMS = "https://cwms-data.usace.army.mil/cwms-data"
CWMS_HEADERS = {"Accept": "application/json;version=2"}
CWMS_LIKE = r".*\.(Flow|Flow-Total|Flow-Out|Flow-Res Out|Flow-Outflow|Stor|Stor-Res|Stor-Total|Elev|Elev-Pool|Elev-Headwater|Elev-Forebay|Elev-Lake|Elev-Res)\..*"
CWMS_MATCH_M = 3_000.0
# Parameter preference within a kind; plain `Flow` counts as a release only at a tailwater/outflow sub-location or at
# the project itself (LRH, LRP, NAE publish releases that way), and plain `Elev` never at a tailwater.
CWMS_PARAMS = {
    "storage": ("Stor", "Stor-Res", "Stor-Total"),
    "elevation": ("Elev-Pool", "Elev-Forebay", "Elev-Headwater", "Elev-Lake", "Elev-Res", "Elev"),
    "outflow": ("Flow-Res Out", "Flow-Out", "Flow-Outflow", "Flow-Total", "Flow"),
}
CWMS_UNITS = {"storage": "ac-ft", "elevation": "ft", "outflow": "cfs"}
TAILWATER = re.compile(r"tail|\btw\b|outflow|-out\b|below|blw", re.IGNORECASE)

LAKE_MATCH_M = 5_000.0
LAKE_STORAGE = (Parameter.RESERVOIR_STORAGE,)
LAKE_ELEVATION = (Parameter.RESERVOIR_ELEVATION, Parameter.LAKE_ELEVATION_NGVD29, Parameter.LAKE_ELEVATION_NAVD88,
                  Parameter.LAKE_ELEVATION_LOCAL, Parameter.LAKE_ELEVATION_OTHER)
NLDI = "https://api.water.usgs.gov/nldi/linked-data"
RELEASE_MAX_KM = 15.0
NEST_MAX_KM = 600.0

RESOPS_MATCH_M = 3_000.0
NYC_SOURCE = "s3://flowcast-training-257129854363/datasets/reservoirs-sources/nyc_daily_usable_storage_mg.csv"
# usable capacity, million gallons (NYC DEP), as in the dam-release oracle work (model/.../reservoirs.py, PR #34)
NYC_CAPACITY_MG = {"cannonsville": 95_706.0, "pepacton": 140_190.0, "neversink": 34_941.0}
NYC_DAM_NAMES = {"cannonsville": "Cannonsville", "pepacton": "Downsville|Pepacton", "neversink": "Neversink"}

RISE = "https://data.usbr.gov/rise/api/location"
CDEC_BBOX = (-125.0, 32.0, -114.0, 42.5)  # California: every CDEC reservoir station is west of the basins

FEATURES = ("res_fill", "res_fill_cov", "res_fill_d24", "res_release_mm_h", "res_release_cov", "res_fill_avail", "res_release_avail")


# ------------------------------------------------------------------ dams


def basin_dams(root: Path, cam: Path, basins: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dams with capacity >= 1,000 acre-ft, one row each; (nid_id, STAID) pairs), matched like regulation.py."""
    attrs = camelsh.attributes(cam)
    polys = camelsh.boundaries(cam)
    dams = regulation.load_nid(regulation.download_nid(root.parent / "nid" / "nation.csv"))
    dams["capacity_af"] = np.where(dams["normal_storage_af"] > 0, dams["normal_storage_af"], dams["nid_storage_af"])
    areas = attrs.loc[basins, "DRAIN_SQKM"].astype(float)
    points = gpd.GeoSeries(gpd.points_from_xy(attrs.loc[basins, "LNG_GAGE"], attrs.loc[basins, "LAT_GAGE"]), index=basins, crs="EPSG:4326")
    first = regulation.dams_in_basins(dams, polys.loc[basins], areas, points)
    ringed = sorted(first.loc[first["ring"], "STAID"].unique())
    joined = regulation.dams_in_basins(dams, polys.loc[basins], areas, points, traveltime.networks(root / "nldi", ringed))
    joined = joined[joined["capacity_af"] >= MIN_CAPACITY_AF]
    pairs = joined[["nid_id", "STAID"]].reset_index(drop=True)
    table = joined.drop_duplicates("nid_id").set_index("nid_id")[["name", "capacity_af", "normal_storage_af", "nid_storage_af", "drainage_km2", "lat", "lon", "primary_purpose"]]
    return table, pairs


def _near(dams: pd.DataFrame, pts: gpd.GeoDataFrame, meters: float) -> pd.DataFrame:
    """Nearest station within `meters` of each dam (NaN columns where none)."""
    a = gpd.GeoDataFrame(dams, geometry=gpd.points_from_xy(dams["lon"], dams["lat"]), crs="EPSG:4326").to_crs(EQUAL_AREA)
    j = gpd.sjoin_nearest(a, pts.to_crs(EQUAL_AREA), how="left", max_distance=meters, distance_col="dist_m")
    return j[~j.index.duplicated()]


# ------------------------------------------------------------------ CWMS


def _cwms_get(path: str, params: dict, timeout: int = 300) -> dict:
    for attempt in range(5):
        try:
            resp = requests.get(f"{CWMS}/{path}", params=params, headers=CWMS_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return {}
        except (requests.RequestException, ValueError):
            pass
        log.info("CWMS %s retry %d", path, attempt + 1)
    raise RuntimeError(f"CWMS {path} {params} failed")


def cwms_offices() -> list[str]:
    offices = _cwms_get("offices", {"format": "json"})
    rows = offices.get("offices", {}).get("offices", offices) if isinstance(offices, dict) else offices
    names = [o["name"] for o in rows if isinstance(o, dict) and o.get("name")]
    return sorted(n for n in names if re.fullmatch(r"[A-Z]{3}|NWDP|NWDM", n))


def cwms_catalog(cache: Path) -> pd.DataFrame:
    """Every CWMS series of the parameters above, with its location's coordinates and extents (all offices)."""
    path = cache / "cwms_catalog.parquet"
    if path.exists():
        return pd.read_parquet(path)
    rows, coords = [], {}
    for office in cwms_offices():
        page = None
        while True:
            j = _cwms_get("catalog/TIMESERIES", {"office": office, "like": CWMS_LIKE, "page-size": 5000} | ({"page": page} if page else {}))
            for e in j.get("entries", []):
                ext = e.get("extents") or []
                rows.append({
                    "office": office, "name": e["name"],
                    "earliest": min((x["earliest-time"] for x in ext if x.get("earliest-time")), default=None),
                    "latest": max((x["latest-time"] for x in ext if x.get("latest-time")), default=None),
                })
            if not (page := j.get("next-page")):
                break
        locs = _cwms_get("catalog/LOCATIONS", {"office": office, "page-size": 20000}).get("entries", [])
        for e in locs:
            if e.get("latitude") and e.get("longitude"):
                coords[(office, e["name"])] = (float(e["latitude"]), float(e["longitude"]))
        log.info("CWMS %s: %d series so far", office, len(rows))
    df = pd.DataFrame(rows)
    parts = df["name"].str.split(".", expand=True)
    df["location"], df["param"], df["type"], df["interval"], df["version"] = parts[0], parts[1], parts[2], parts[3], parts[5]
    df["base"] = df["location"].str.split("-").str[0]
    xy = [coords.get((o, loc)) or coords.get((o, b)) for o, loc, b in zip(df["office"], df["location"], df["base"])]
    df["lat"] = [p[0] if p else np.nan for p in xy]
    df["lon"] = [p[1] if p else np.nan for p in xy]
    for c in ("earliest", "latest"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    df.to_parquet(path)
    return df


def _cwms_kind(param: str, location: str) -> str | None:
    for kind, params in CWMS_PARAMS.items():
        if param in params:
            if param == "Flow" and "-" in location and not TAILWATER.search(location):
                return None
            if param == "Elev" and TAILWATER.search(location):
                return None
            return kind
    return None


def cwms_choice(catalog: pd.DataFrame, dams: pd.DataFrame) -> pd.DataFrame:
    """Per dam and kind: the live series used (sub-daily preferred) and an optional longer history series."""
    cat = catalog.dropna(subset=["lat", "lon"]).copy()
    ver = cat["version"].str.lower()
    cat = cat[~ver.str.contains("fcst|forecast|project|rfc|nws|wfo|chips|smooth|ai2|py3|goes|raw")]
    cat["kind"] = [_cwms_kind(p, loc) for p, loc in zip(cat["param"], cat["location"])]
    cat = cat[cat["kind"].notna() & cat["interval"].isin(["15Minutes", "30Minutes", "1Hour", "1Day", "~1Day"])]
    cat = cat[cat["type"].isin(["Inst", "Ave"])]
    stations = cat.groupby(["office", "base"]).agg(lat=("lat", "first"), lon=("lon", "first")).reset_index()
    near = _near(dams, gpd.GeoDataFrame(stations, geometry=gpd.points_from_xy(stations["lon"], stations["lat"]), crs="EPSG:4326"), CWMS_MATCH_M)
    out = []
    for nid, row in near.dropna(subset=["base"]).iterrows():
        series = cat[(cat["office"] == row["office"]) & (cat["base"] == row["base"])]
        for kind, params in CWMS_PARAMS.items():
            s = series[series["kind"] == kind].copy()
            if s.empty:
                continue
            s["prank"] = s["param"].map({p: i for i, p in enumerate(params)})
            s["daily"] = s["interval"].str.contains("Day")
            live = s[s["latest"] >= ACTIVE_SINCE].sort_values(["prank", "daily", "earliest"])
            if live.empty:
                continue
            best = live.iloc[0]
            hist = s[(s["param"] == best["param"]) & (s["earliest"] < best["earliest"]) & (s["name"] != best["name"])].sort_values("earliest")
            out.append({
                "nid_id": nid, "kind": kind, "office": row["office"], "series": best["name"], "daily": bool(best["daily"]),
                "history": hist.iloc[0]["name"] if len(hist) else None, "history_daily": bool(hist.iloc[0]["daily"]) if len(hist) else None,
                "dist_m": float(row["dist_m"]),
            })
    return pd.DataFrame(out)


def cwms_series(cache: Path, office: str, name: str, unit: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """All values of one series (UTC index), fetched by year and cached."""
    path = cache / "cwms" / f"{office}.{name}.parquet".replace("/", "_")
    if path.exists():
        return pd.read_parquet(path)["value"]
    frames = []
    for year in range(start.year, end.year + 1):
        j = _cwms_get("timeseries", {"name": name, "office": office, "unit": "EN", "page-size": 500_000,
                                     "begin": f"{year}-01-01T00:00:00Z", "end": f"{year + 1}-01-01T00:00:00Z"}, timeout=600)
        vals = j.get("values") or []
        if vals:
            if j.get("units") and j["units"] != unit:
                raise ValueError(f"{name}: units {j['units']}, expected {unit}")
            a = np.array([(v[0], np.nan if v[1] is None else v[1]) for v in vals], dtype=float)
            frames.append(pd.Series(a[:, 1], index=pd.to_datetime(a[:, 0], unit="ms", utc=True)))
    s = pd.concat(frames) if frames else pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    s = s[~s.index.duplicated()].sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    s.rename("value").to_frame().to_parquet(path)
    return s


# ------------------------------------------------------------------ USGS


def usgs_lakes(client: WaterDataClient, cache: Path) -> gpd.GeoDataFrame:
    """Active USGS reservoir storage / lake elevation series (one row per site and parameter)."""
    path = cache / "usgs_lakes.parquet"
    if not path.exists():
        rows = []
        for pc in (*LAKE_STORAGE, *LAKE_ELEVATION):
            client_rows = client._features("time-series-metadata", {"parameter_code": pc.value, "skipGeometry": "false",
                                                                      "properties": "monitoring_location_id,computation_period_identifier,statistic_id,begin_utc,end_utc"})
            for r in client_rows:
                rows.append({"site": r["monitoring_location_id"], "pc": pc.value, "period": r.get("computation_period_identifier"),
                             "stat": r.get("statistic_id"), "begin": r.get("begin_utc"), "end": r.get("end_utc")})
        meta = pd.DataFrame(rows)
        sites = sorted(meta["site"].unique())
        locs = []
        for i in range(0, len(sites), 200):
            feats = requests.get("https://api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations/items",
                                 params={"id": ",".join(sites[i:i + 200]), "f": "json", "limit": 1000, "properties": "id"},
                                 headers=_usgs_headers(client), timeout=300).json().get("features", [])
            locs += [(f["id"], *f["geometry"]["coordinates"][:2]) for f in feats if f.get("geometry")]
        xy = pd.DataFrame(locs, columns=["site", "lon", "lat"])
        meta.merge(xy, on="site").to_parquet(path)
    df = pd.read_parquet(path)
    for c in ("begin", "end"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    df = df[(df["end"] >= ACTIVE_SINCE) & (df["period"] == "Daily") & (df["stat"] == "00003")]
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


def _usgs_headers(client: WaterDataClient) -> dict:
    return {"X-Api-Key": client.api_key} if client.api_key else {}


def lake_choice(lakes: gpd.GeoDataFrame, dams: pd.DataFrame) -> pd.DataFrame:
    """Per dam: the nearest active lake gauge within 5 km, storage (00054) preferred over elevation."""
    out = []
    for kind, codes in (("storage", LAKE_STORAGE), ("elevation", LAKE_ELEVATION)):
        sub = lakes[lakes["pc"].isin([c.value for c in codes])].copy()
        sub["rank"] = sub["pc"].map({c.value: i for i, c in enumerate(codes)})
        sub = sub.sort_values(["site", "rank", "begin"]).drop_duplicates("site")
        near = _near(dams, sub, LAKE_MATCH_M).dropna(subset=["site"])
        out += [{"nid_id": nid, "kind": kind, "site": r["site"], "pc": r["pc"], "dist_m": float(r["dist_m"])} for nid, r in near.iterrows()]
    return pd.DataFrame(out)


def usgs_daily(client: WaterDataClient, site: str, pc: str) -> pd.Series:
    df = client.daily(site, Parameter(pc), "2000-01-01", pd.Timestamp.now().strftime("%Y-%m-%d"))
    s = pd.Series(pd.to_numeric(df["value"], errors="coerce").to_numpy(), index=pd.DatetimeIndex(df["date"]))
    return s[~s.index.duplicated()].sort_index()


def active_discharge_sites(client: WaterDataClient, cache: Path) -> set[str]:
    path = cache / "usgs_q_points.parquet"
    if not path.exists():
        rows = client._features("time-series-metadata", {"parameter_code": "00060", "computation_period_identifier": "Points",
                                                          "properties": "monitoring_location_id,end_utc"})
        pd.DataFrame([(r["monitoring_location_id"], r.get("end_utc")) for r in rows], columns=["site", "end"]).to_parquet(path)
    df = pd.read_parquet(path)
    return set(df.loc[pd.to_datetime(df["end"], utc=True, errors="coerce") >= ACTIVE_SINCE, "site"].str.removeprefix("USGS-"))


def dam_network(dams: pd.DataFrame, active: set[str], cache: Path) -> pd.DataFrame:
    """Per dam (NLDI): its NHDPlus COMID, the COMIDs downstream on the main stem (for nesting), and the nearest active
    USGS discharge gauge at most 15 km downstream with that gauge's COMID."""
    path = cache / "dam_network.json"
    known = json.loads(path.read_text()) if path.exists() else {}
    session = requests.Session()

    def one(item):
        nid, lat, lon = item
        try:
            comid = int(session.get(f"{NLDI}/comid/position", params={"coords": f"POINT({lon} {lat})"}, timeout=60).json()["features"][0]["properties"]["comid"])
            down = session.get(f"{NLDI}/comid/{comid}/navigation/DM/flowlines", params={"distance": NEST_MAX_KM}, timeout=120).json().get("features", [])
            sites = session.get(f"{NLDI}/comid/{comid}/navigation/DM/nwissite", params={"distance": RELEASE_MAX_KM}, timeout=60).json().get("features", [])
        except (requests.RequestException, KeyError, IndexError, ValueError):
            return nid, None
        best = None
        for f in sites:
            site = f["properties"]["identifier"].removeprefix("USGS-")
            if site not in active:
                continue
            glon, glat = f["geometry"]["coordinates"][:2]
            km = float(np.hypot((glon - lon) * 111.32 * np.cos(np.radians(lat)), (glat - lat) * 110.57))
            if km <= RELEASE_MAX_KM and (best is None or km < best[1]):
                best = (site, km, int(f["properties"].get("comid") or 0))
        return nid, {"comid": comid, "down": [int(f["properties"]["nhdplus_comid"]) for f in down],
                     "gauge": best[0] if best else "", "gauge_km": best[1] if best else None, "gauge_comid": best[2] if best else None}

    todo = [(nid, r["lat"], r["lon"]) for nid, r in dams.iterrows() if nid not in known]
    with ThreadPoolExecutor(6) as pool:
        for nid, res in pool.map(one, todo):
            if res is not None:
                known[nid] = res
    path.write_text(json.dumps(known))
    return pd.DataFrame.from_dict(known, orient="index").reindex(dams.index)


# ------------------------------------------------------------------ other sources


def resops(zip_path: Path, dams: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """ResOpsUS dams within 3 km of a basin dam, and their daily storage (acre-ft) and outflow (m3/s)."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        attrs = pd.read_csv(z.open(next(n for n in names if n.endswith("attributes/reservoir_attributes.csv"))))
        near = _near(dams, gpd.GeoDataFrame(attrs, geometry=gpd.points_from_xy(attrs["LONG"], attrs["LAT"]), crs="EPSG:4326"), RESOPS_MATCH_M)
        near = near.dropna(subset=["DAM_ID"])
        series = {}
        for nid, row in near.iterrows():
            member = next((n for n in names if n.endswith(f"time_series_all/ResOpsUS_{int(row['DAM_ID'])}.csv")), None)
            if member is None:
                continue
            ts = pd.read_csv(z.open(member), parse_dates=["date"]).set_index("date")
            # ResOpsUS storage is million m3
            series[nid] = pd.DataFrame({"storage_af": ts["storage"] * 1e6 / ACRE_FT_M3, "outflow_m3s": ts["outflow"]})
    return near[["DAM_ID"]], series


def nyc_storage() -> pd.DataFrame:
    """Daily usable storage (MG) of the NYC Delaware reservoirs, merged by the dam-release oracle work."""
    df = pd.read_csv(NYC_SOURCE, index_col=0, parse_dates=True, storage_options={"anon": False})
    df.index = pd.DatetimeIndex(df.index).normalize()
    return df[~df.index.duplicated()].sort_index()


def other_feeds(dams: pd.DataFrame) -> dict[str, int]:
    """Stations of USBR RISE, TVA and CDEC within 3 km of a basin dam (expected: none in HUC2 01/02/04/05)."""
    out = {}
    lon0, lat0, lon1, lat1 = dams["lon"].min() - 1, dams["lat"].min() - 1, dams["lon"].max() + 1, dams["lat"].max() + 1
    pts, page = [], 1
    while True:
        j = requests.get(RISE, params={"page": page, "itemsPerPage": 1000}, headers={"Accept": "application/vnd.api+json"}, timeout=120).json()
        for item in j.get("data", []):
            coords = (item.get("attributes", {}).get("locationCoordinates") or {}).get("coordinates")
            if coords and len(coords) >= 2:
                pts.append((float(coords[0]), float(coords[1])))
        if len(j.get("data", [])) < 1000:
            break
        page += 1
    rise = pd.DataFrame(pts, columns=["lon", "lat"])
    rise = rise[rise["lon"].between(lon0, lon1) & rise["lat"].between(lat0, lat1)]
    out["rise_locations_total"] = len(pts)
    out["rise_near_basin_dams"] = int(_near(dams, gpd.GeoDataFrame(rise, geometry=gpd.points_from_xy(rise["lon"], rise["lat"]), crs="EPSG:4326"), CWMS_MATCH_M)["dist_m"].notna().sum()) if len(rise) else 0
    # TVA projects are all in HUC2 06 (Tennessee); CDEC stations are in California.
    out["tva_dams"] = int(dams["name"].str.contains("TVA|Tennessee Valley", case=False).sum())
    out["cdec_dams"] = int((dams["lon"].between(CDEC_BBOX[0], CDEC_BBOX[2]) & dams["lat"].between(CDEC_BBOX[1], CDEC_BBOX[3])).sum())
    return out


# ------------------------------------------------------------------ per-dam hourly series


def daily_to_hourly(daily: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    """Value of local day D at every hour-ending time of local day D+1."""
    if daily.empty:
        return np.full(len(index), np.nan, dtype=np.float32)
    d = daily.copy()
    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize() if d.index.tz is None else d.index.tz_convert(TIMEZONE).tz_localize(None).normalize()
    d = d[~d.index.duplicated(keep="last")]
    local = (index - pd.Timedelta(hours=1)).tz_convert(TIMEZONE).tz_localize(None).normalize()
    return d.reindex(local - pd.Timedelta(days=1)).to_numpy(np.float32)


def subdaily_to_hourly(s: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    """Hour-ending mean over (t-1 h, t]."""
    if s.empty:
        return np.full(len(index), np.nan, dtype=np.float32)
    h = s.groupby(s.index.ceil("h")).mean()
    return h.reindex(index).to_numpy(np.float32)


def merge(parts: list[np.ndarray]) -> np.ndarray:
    """First non-NaN value across `parts`, in order of preference."""
    out = np.full_like(parts[0], np.nan)
    for p in parts:
        out = np.where(np.isnan(out), p, out)
    return out


@dataclass
class DamSeries:
    """Hourly fill and release parts of one dam. Release parts are kept apart because a release gauge only counts
    for basins it is upstream of; `release_backfill` (ResOpsUS) is used only together with a live release."""

    fill: np.ndarray
    fill_kind: str = ""
    fill_sources: list[str] = field(default_factory=list)
    release_cwms: np.ndarray | None = None
    release_gauge: np.ndarray | None = None
    release_backfill: np.ndarray | None = None

    def release(self, gauge_ok: bool) -> np.ndarray | None:
        live = [p for p in (self.release_cwms, self.release_gauge if gauge_ok else None) if p is not None]
        if not live:
            return None
        return merge(live + ([self.release_backfill] if self.release_backfill is not None else []))


def pool_index(elev: np.ndarray, before: np.ndarray) -> np.ndarray:
    ref = elev[before & np.isfinite(elev)]
    if ref.size < 24 * 180:
        return np.full_like(elev, np.nan)
    lo, hi = np.percentile(ref, POOL_PCT)
    return ((elev - lo) / max(hi - lo, 1e-3)).astype(np.float32)


# ------------------------------------------------------------------ basin features


def basin_features(
    basins: list[str], areas: np.ndarray, pairs: pd.DataFrame, dams: pd.DataFrame, series: dict[str, DamSeries],
    network: pd.DataFrame, basin_comids: dict[str, set[int]], n_hours: int,
) -> dict[str, np.ndarray]:
    """(basin, time) features. `basin_comids`: each basin's NHDPlus upstream COMIDs, for release gauges."""
    out = {f: np.full((len(basins), n_hours), np.nan, dtype=np.float32) for f in FEATURES}
    for f in ("res_fill_cov", "res_release_cov", "res_fill_avail", "res_release_avail"):
        out[f][:] = 0.0
    by_basin = pairs.groupby("STAID")["nid_id"].apply(list).to_dict()
    for i, b in enumerate(basins):
        ids = by_basin.get(b, [])
        if not ids:
            continue
        cap = dams.loc[ids, "capacity_af"].to_numpy(float)
        total = cap.sum()
        fill = np.stack([series[d].fill for d in ids]) if ids else None
        have = np.isfinite(fill)
        w = np.where(have, cap[:, None], 0.0)
        wsum = w.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out["res_fill"][i] = np.where(wsum > 0, (np.nan_to_num(fill) * w).sum(axis=0) / wsum, np.nan)
            out["res_fill_cov"][i] = wsum / total
            prev = np.full_like(fill, np.nan)
            prev[:, 24:] = fill[:, :-24]
            both = have & np.isfinite(prev)
            wb = np.where(both, cap[:, None], 0.0)
            out["res_fill_d24"][i] = np.where(wb.sum(axis=0) > 0, (np.nan_to_num(fill - prev) * wb).sum(axis=0) / wb.sum(axis=0), np.nan)
        out["res_fill_avail"][i] = (wsum > 0).astype(np.float32)

        # Releases: a dam's gauge counts only if it is upstream of this basin's gauge (not the gauge itself).
        rel = {}
        for d in ids:
            gauge = network.loc[d, "gauge"] if d in network.index else ""
            ok = bool(gauge) and gauge != b and network.loc[d, "gauge_comid"] in basin_comids.get(b, set())
            s = series[d].release(ok)
            if s is not None and np.isfinite(s).any():
                rel[d] = s
        comid = {d: network.loc[d, "comid"] for d in ids if d in network.index and pd.notna(network.loc[d, "comid"])}
        down = {d: set(network.loc[d, "down"]) if d in network.index and isinstance(network.loc[d, "down"], list) else set() for d in ids}
        below = {d: [k for k in rel if k != d and k in comid and comid[k] in down[d]] for d in ids}  # observed dams downstream of d
        total_rel = np.zeros(n_hours, dtype=np.float64)
        n_rel = np.zeros(n_hours, dtype=np.int32)
        covered = np.zeros((len(ids), n_hours), dtype=bool)
        for d, s in rel.items():
            shadow = np.zeros(n_hours, dtype=bool)
            for k in below[d]:
                shadow |= np.isfinite(rel[k])
            use = np.isfinite(s) & ~shadow
            total_rel += np.where(use, s, 0.0)
            n_rel += use
        for j, d in enumerate(ids):
            c = np.isfinite(rel[d]) if d in rel else np.zeros(n_hours, dtype=bool)
            for k in below[d]:
                c |= np.isfinite(rel[k])
            covered[j] = c
        out["res_release_mm_h"][i] = np.where(n_rel > 0, total_rel * 3.6 / areas[i], np.nan)
        out["res_release_cov"][i] = (covered * cap[:, None]).sum(axis=0) / total
        out["res_release_avail"][i] = (n_rel > 0).astype(np.float32)
    return out


# ------------------------------------------------------------------ build


def work(root: Path) -> Path:
    path = root / "v14"
    path.mkdir(parents=True, exist_ok=True)
    return path


def client(root: Path) -> WaterDataClient:
    return WaterDataClient(cache=ResponseCache(root.parent / "usgs-cache"), max_retries=8)


def discover(root: Path, cam: Path, resops_zip: Path) -> pd.DataFrame:
    """Match every basin dam to its sources; writes v14/dams.parquet, v14/pairs.parquet and v14/sources.json."""
    cache = work(root)
    basins = list(pd.read_parquet(root / "selection.parquet").index)
    dams, pairs = basin_dams(root, cam, basins)
    log.info("%d dams >= %.0f acre-ft in %d basins", len(dams), MIN_CAPACITY_AF, pairs["STAID"].nunique())
    c = client(root)
    cw = cwms_choice(cwms_catalog(cache), dams)
    lk = lake_choice(usgs_lakes(c, cache), dams)
    net = dam_network(dams, active_discharge_sites(c, cache), cache)
    ro_ids, _ = resops(resops_zip, dams)
    table = dams.copy()
    for kind in ("storage", "elevation", "outflow"):
        sub = cw[cw["kind"] == kind].set_index("nid_id") if len(cw) else pd.DataFrame()
        table[f"cwms_{kind}"] = sub["series"].reindex(table.index) if len(sub) else None
        table[f"cwms_{kind}_office"] = sub["office"].reindex(table.index) if len(sub) else None
        table[f"cwms_{kind}_daily"] = sub["daily"].reindex(table.index) if len(sub) else None
        table[f"cwms_{kind}_history"] = sub["history"].reindex(table.index) if len(sub) else None
    for kind in ("storage", "elevation"):
        sub = lk[lk["kind"] == kind].set_index("nid_id") if len(lk) else pd.DataFrame()
        table[f"usgs_{kind}_site"] = sub["site"].reindex(table.index) if len(sub) else None
        table[f"usgs_{kind}_pc"] = sub["pc"].reindex(table.index) if len(sub) else None
    table["nyc"] = ""
    for key, pattern in NYC_DAM_NAMES.items():
        hit = table["name"].str.contains(pattern, case=False) & table["lat"].between(41.5, 42.5) & table["lon"].between(-75.5, -74.3)
        table.loc[hit, "nyc"] = key
    table["resops_id"] = ro_ids["DAM_ID"].reindex(table.index)
    table["comid"] = net["comid"]
    table["release_gauge"] = net["gauge"].fillna("")
    table["release_gauge_km"] = net["gauge_km"]
    table["release_gauge_comid"] = net["gauge_comid"]
    table.to_parquet(cache / "dams.parquet")
    pairs.to_parquet(cache / "pairs.parquet")
    feeds = other_feeds(dams)
    (cache / "sources.json").write_text(json.dumps(feeds))
    log.info("other feeds: %s", feeds)
    return table


def full_index() -> pd.DatetimeIndex:
    return config.hourly_index()


def _cwms_hourly(cache: Path, office: str, name: str | None, daily: bool | None, unit: str, index: pd.DatetimeIndex) -> np.ndarray | None:
    if not isinstance(name, str) or not name:
        return None
    s = cwms_series(cache, office, name, unit, index[0], index[-1])
    s = s[(s > -1e5) & (s < 1e9)]
    return daily_to_hourly(s, index) if daily else subdaily_to_hourly(s, index)


def cube_discharge(subset_root: str, sites: list[str], index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Hourly discharge (m3/s) of basin gauges from the v1.3 full cube, trainval then test."""
    out = {s: np.full(len(index), np.nan, dtype=np.float32) for s in sites}
    if not sites:
        return out
    for store in ("trainval", "test"):
        ds = xr.open_zarr(f"{subset_root}/{store}.zarr", chunks=None, consolidated=None)
        ids = [str(b) for b in ds["basin"].values]
        times = pd.DatetimeIndex(ds["time"].values).tz_localize("UTC")
        pos = index.get_indexer(times)
        for s in sites:
            if s in ids:
                q = ds["qobs_m3s"].isel(basin=ids.index(s)).values.astype(np.float32)
                keep = pos >= 0
                out[s][pos[keep]] = np.where(np.isfinite(q[keep]), q[keep], out[s][pos[keep]])
    return out


def gauge_discharge(root: Path, c: WaterDataClient, site: str, index: pd.DatetimeIndex, cube_q: dict[str, np.ndarray]) -> tuple[np.ndarray, str]:
    """Hourly (m3/s) release-gauge series and its source: the v1.3 cube, CAMELSH + USGS API (2024 on), or USGS daily."""
    if site in cube_q:
        return cube_q[site], "cube"
    cam = cube.camelsh_discharge(root, site)
    if len(cam):
        targets.pull_site(c, site, "discharge", config.USGS_TARGETS_FROM, index[-1] + pd.Timedelta(hours=1), root / "targets")
        usgs = targets.load_usgs(root / "targets", site, "discharge")["value"].astype(np.float32)
        q = pd.Series(np.nan, index=index, dtype=np.float32)
        q.update(cam[cam.index < config.USGS_TARGETS_FROM])
        q.update(usgs[usgs.index >= config.USGS_TARGETS_FROM])
        q[q < 0] = np.nan
        return q.to_numpy(np.float32), "camelsh+usgs"
    daily = usgs_daily(c, site, Parameter.DISCHARGE.value) * CFS_M3S
    return daily_to_hourly(daily[daily >= 0], index), "usgs-daily"


def _frac(x: np.ndarray | None, capacity: float) -> np.ndarray | None:
    return None if x is None else (x / capacity).astype(np.float32)


def one_dam(root: Path, c: WaterDataClient, nid: str, row: pd.Series, index: pd.DatetimeIndex, cube_q: dict[str, np.ndarray],
            nyc: pd.DataFrame | None, ro: dict[str, pd.DataFrame]) -> DamSeries:
    cache = work(root)
    cap = float(row["capacity_af"])
    before = np.asarray(index < config.SPLITS[2].start)
    backfill = ro.get(nid)

    def cwms(kind: str, which: str = "") -> np.ndarray | None:
        name = row.get(f"cwms_{kind}_history" if which else f"cwms_{kind}")
        daily = row.get(f"cwms_{kind}_daily") if not which else bool(re.search(r"Day", str(name)))
        return _cwms_hourly(cache, row.get(f"cwms_{kind}_office"), name, bool(daily), CWMS_UNITS[kind], index)

    storage: list[tuple[str, np.ndarray]] = []
    for label, part in (("cwms", _frac(cwms("storage"), cap)), ("cwms-history", _frac(cwms("storage", "history"), cap))):
        if part is not None:
            storage.append((label, part))
    if row.get("nyc") and nyc is not None:
        key = row["nyc"]
        storage.append(("nyc-dep", (daily_to_hourly(nyc[key], index) / NYC_CAPACITY_MG[key]).astype(np.float32)))
    if isinstance(row.get("usgs_storage_site"), str):
        storage.append(("usgs", _frac(daily_to_hourly(usgs_daily(c, row["usgs_storage_site"], row["usgs_storage_pc"]), index), cap)))
    live = [p for p in storage if p[0] != "cwms-history"]
    series = DamSeries(fill=np.full(len(index), np.nan, dtype=np.float32))
    if live:
        if backfill is not None:
            storage.append(("resops", _frac(daily_to_hourly(backfill["storage_af"], index), cap)))
        series.fill = merge([p for _, p in storage])
        series.fill_kind, series.fill_sources = "storage", [n for n, _ in storage]
    else:
        elev = [p for p in (cwms("elevation"), cwms("elevation", "history")) if p is not None]
        label = "cwms"
        if not elev and isinstance(row.get("usgs_elevation_site"), str):
            elev, label = [daily_to_hourly(usgs_daily(c, row["usgs_elevation_site"], row["usgs_elevation_pc"]), index)], "usgs"
        if elev:
            series.fill = pool_index(merge(elev), before)
            series.fill_kind, series.fill_sources = "pool", [label]
    series.fill = np.where((series.fill > -1.0) & (series.fill < 3.0), series.fill, np.nan).astype(np.float32)

    out = cwms("outflow")
    if out is not None:
        hist = cwms("outflow", "history")
        series.release_cwms = (merge([out, hist]) if hist is not None else out) * CFS_M3S
    if row.get("release_gauge"):
        series.release_gauge, _ = gauge_discharge(root, c, row["release_gauge"], index, cube_q)
    if backfill is not None:
        series.release_backfill = daily_to_hourly(backfill["outflow_m3s"], index)
    for name in ("release_cwms", "release_gauge", "release_backfill"):
        part = getattr(series, name)
        if part is not None:
            setattr(series, name, np.where(part >= 0, part, np.nan).astype(np.float32))
    return series


def dam_series(root: Path, table: pd.DataFrame, index: pd.DatetimeIndex, cube_q: dict[str, np.ndarray], resops_zip: Path) -> dict[str, DamSeries]:
    c = client(root)
    nyc = nyc_storage() if (table["nyc"] != "").any() else None
    _, ro = resops(resops_zip, table)
    out: dict[str, DamSeries] = {}

    def run(nid: str) -> tuple[str, DamSeries]:
        return nid, one_dam(root, c, nid, table.loc[nid], index, cube_q, nyc, ro)

    with ThreadPoolExecutor(8) as pool:
        for k, (nid, s) in enumerate(pool.map(run, table.index), 1):
            out[nid] = s
            if k % 50 == 0:
                log.info("dam series %d/%d", k, len(table))
    return out


def dam_summary(table: pd.DataFrame, series: dict[str, DamSeries], index: pd.DatetimeIndex) -> pd.DataFrame:
    train = np.asarray((index >= config.SPLITS[0].start) & (index <= config.SPLITS[0].end))
    recent = np.asarray(index >= index[-1] - pd.Timedelta(days=45))
    rows = {}
    for nid, s in series.items():
        rel = s.release(True)
        rows[nid] = {
            "fill_kind": s.fill_kind, "fill_sources": ",".join(s.fill_sources),
            "fill_train_frac": float(np.isfinite(s.fill[train]).mean()), "fill_recent": bool(np.isfinite(s.fill[recent]).any()),
            "release_sources": ",".join(n for n in ("cwms", "gauge", "backfill") if getattr(s, f"release_{n}") is not None),
            "release_train_frac": float(np.isfinite(rel[train]).mean()) if rel is not None else 0.0,
            "release_recent": bool(np.isfinite(rel[recent]).any()) if rel is not None else False,
        }
    return table.join(pd.DataFrame.from_dict(rows, orient="index"))


FEATURE_ATTRS = {
    "res_fill": {"units": "1", "description": "capacity-weighted fill of the upstream reservoirs (dams >= 1,000 acre-ft) that report: storage / NID capacity, or a relative pool index (0 = 5th, 1 = 95th percentile of the pre-2022-10 record) where only pool elevation is published; NaN where none reports"},
    "res_fill_cov": {"units": "1", "description": "share of the basin's upstream reservoir capacity with a fill value at t; 0 for basins without such dams"},
    "res_fill_d24": {"units": "1/day", "description": "capacity-weighted 24 h change in fill over dams reporting at t and t-24 h"},
    "res_release_mm_h": {"units": "mm/h", "description": "observed release of the upstream reservoirs over the basin area, summing only the most downstream reporting dam of any nested chain; CWMS outflow, an active USGS gauge <= 15 km below the dam and upstream of this basin's gauge, or ResOpsUS daily outflow (backfill); NaN where none reports"},
    "res_release_cov": {"units": "1", "description": "share of upstream reservoir capacity whose release is observed at t (the dam's own or a downstream reporting dam's)"},
    "res_fill_avail": {"units": "1", "description": "1 where res_fill is defined, else 0"},
    "res_release_avail": {"units": "1", "description": "1 where res_release_mm_h is defined, else 0"},
}


def write_subset(root: Path, subset: str, basins: list[str], rows: np.ndarray, feats: dict[str, np.ndarray], statics: pd.DataFrame,
                 index: pd.DatetimeIndex, manifest_extra: dict, upload: bool) -> str:
    """Write `v1.4/{subset}/{trainval,test}.zarr` (only the v1.4 arrays; read next to the v1.3 stores) and the manifest."""
    out = root / "cube" / "v1.4" / subset
    train = config.SPLITS[0]
    manifest = {
        "version": "v1.4", "subset": subset, "created": pd.Timestamp.now(tz="UTC").isoformat(), "git_sha": cube.git_sha(),
        "basins": basins, "read_with": f"s3://{config.BUCKET}/v1.3/{subset}/", "stores": {}, **manifest_extra,
    }
    for store, (start, end) in config.STORES.items():
        sl = np.asarray((index >= start) & (index <= end))
        times = index[sl]
        train_mask = np.asarray((times >= train.start) & (times <= train.end))
        path = out / f"{store}.zarr"
        if path.exists():
            shutil.rmtree(path)
        group = zarr.open_group(path, mode="w", zarr_format=3)
        cube._create(group, "basin", cube.basin_ids(basins), ("basin",), {}, basin_axis=False)
        cube._create(group, "time", cube._hours(times), ("time",), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}, basin_axis=False)
        stats = {}
        for name, data in feats.items():
            block = data[rows][:, sl]
            rs = cube.RunningStats(train_mask, 1)
            rs.add(block)
            stats[name] = rs.result()
            cube._create(group, name, block, ("basin", "time"), FEATURE_ATTRS[name] | {"source": "reservoirs.py (v1.4)"} | stats[name])
        for col in statics.columns:
            cube._create(group, col, statics[col].to_numpy(np.float32)[rows], ("basin",), STATIC_ATTRS.get(col, {}), basin_axis=False)
        group.attrs.update({
            "flowcast_schema": "flowcast-training-cube", "version": "v1.4", "subset": subset, "store": store,
            "store_period": [str(start), str(end)], "splits": {s.name: [str(s.start), str(s.end)] for s in config.SPLITS},
            "description": "v1.4 reservoir storage and observed-release arrays; open next to the v1.3 store of the same subset",
            "time_convention": "hour-ending: value at t covers (t-1h, t]; daily sources use the previous local day's value",
        })
        zarr.consolidate_metadata(path)
        manifest["stores"][store] = {"sha256": cube.tree_hash(path), "bytes": sum(f.stat().st_size for f in path.rglob("*") if f.is_file()), "stats": stats}
    text = json.dumps(manifest, indent=1, default=str)
    (out / "manifest.json").write_text(text)
    manifest_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    (out / "MANIFEST_ID").write_text(manifest_id + "\n")
    if upload:
        subprocess.run(["aws", "s3", "sync", "--only-show-errors", "--delete", str(out) + "/", f"s3://{config.BUCKET}/v1.4/{subset}/"], check=True)
    log.info("v1.4 %s written (manifest %s)", subset, manifest_id)
    return manifest_id


STATIC_ATTRS = {
    "res_capacity_mm": {"units": "mm", "description": "capacity of the upstream reservoirs >= 1,000 acre-ft (NID normal, or maximum for dry dams) over the basin area"},
    "res_n_dams": {"units": "1", "description": "number of upstream reservoirs >= 1,000 acre-ft"},
}


def basin_upstream_comids(root: Path, sites: list[str]) -> dict[str, set[int]]:
    return {s: set(traveltime.flowlines(root / "nldi", s)["comid"].astype(int)) for s in sites}


def build(root: Path, resops_zip: Path, subsets: list[str], upload: bool = True) -> dict[str, str]:
    """Per-dam series -> basin features -> v1.4 stores for each subset. Needs `discover` first."""
    cache = work(root)
    table = pd.read_parquet(cache / "dams.parquet")
    pairs = pd.read_parquet(cache / "pairs.parquet")
    network = pd.DataFrame.from_dict(json.loads((cache / "dam_network.json").read_text()), orient="index").reindex(table.index)
    sel = pd.read_parquet(root / "selection.parquet")
    basins = list(sel.index)
    areas = sel["DRAIN_SQKM"].astype(float).to_numpy()
    index = full_index()
    gauges = sorted({g for g in table["release_gauge"] if g})
    cube_q = cube_discharge(f"s3://{config.BUCKET}/v1.3/full", [g for g in gauges if g in set(basins)], index)
    series = dam_series(root, table, index, cube_q, resops_zip)
    summary = dam_summary(table, series, index)
    summary.to_parquet(cache / "dam_summary.parquet")
    with_gauge = sorted(pairs.loc[pairs["nid_id"].isin(table.index[table["release_gauge"] != ""]), "STAID"].unique())
    feats = basin_features(basins, areas, pairs, table, series, network, basin_upstream_comids(root, with_gauge), len(index))
    by_basin = pairs.merge(table[["capacity_af"]], left_on="nid_id", right_index=True).groupby("STAID")["capacity_af"]
    statics = pd.DataFrame({
        "res_capacity_mm": by_basin.sum().reindex(basins).fillna(0.0).to_numpy() * ACRE_FT_M3 / (areas * 1e6) * 1000.0,
        "res_n_dams": by_basin.size().reindex(basins).fillna(0).to_numpy(float),
    }, index=basins)
    sources = json.loads((cache / "sources.json").read_text())
    extra = {"dams": len(table), "other_feeds": sources, "min_capacity_af": MIN_CAPACITY_AF}
    ids = {}
    early = json.loads((root / "slice.json").read_text())
    for subset in subsets:
        members = early if subset == "slice50" else basins
        rows = np.array([basins.index(b) for b in members])
        ids[subset] = write_subset(root, subset, members, rows, feats, statics, index, extra, upload)
    if upload:
        subprocess.run(["aws", "s3", "cp", "--only-show-errors", str(cache / "dam_summary.parquet"), f"s3://{config.BUCKET}/v1.4/dams.parquet"], check=True)
    return ids
