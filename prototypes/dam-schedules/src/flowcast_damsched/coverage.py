"""How much of the regulation in the training basins could schedules, release gauges and storage data cover?

For every NID dam (main structure, normal storage >= 1,000 acre-ft) inside a training basin's NLDI polygon,
flag which sources reach it:

* CWMS (USACE Data API): operator forecast outflow, observed outflow, storage, pool elevation; history start.
* USGS reservoir gauges: storage (00054) or lake/reservoir elevation (00062, 62614, 62615, 62616, 72275).
* Brookfield Safe Waters facilities (schedules plus operator-reported flow and pool).
* Delaware sources already archived (ODRM, NYC DEP).
* ResOpsUS (daily history to 2020, no real-time feed).
* An active USGS discharge gauge downstream on the main stem within 15 km (NLDI), i.e. an observed release.

Stations are matched to dams by distance (3 km for release points and CWMS projects, 5 km for lake gauges,
since a lake gauge can sit anywhere on the reservoir). Shares are by normal storage, each dam counted once.
All inputs are cached under `cache_dir`; the first run downloads about 100 MB (mostly the NID).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from remotezip import RemoteZip
from shapely.geometry import shape

from .sources import safewaters

log = logging.getLogger(__name__)

NID_URL = "https://nid.sec.usace.army.mil/api/nation/csv"
NLDI = "https://api.water.usgs.gov/nldi/linked-data"
OGC = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
CWMS = "https://cwms-data.usace.army.mil/cwms-data"
RESOPS_ZIP = "https://zenodo.org/api/records/6612040/files/ResOpsUS 2.zip/content"
EQUAL_AREA = "EPSG:5070"
MIN_STORAGE_AF = 1_000.0
# Districts whose areas touch HUC2 01, 02, 04 and 05, where the training basins are.
CWMS_OFFICES = ("NAE", "NAN", "NAP", "NAB", "NAO", "LRB", "LRE", "LRC", "LRP", "LRH", "LRL", "LRN")
# Districts name releases differently: Flow-Out / Flow-Res Out at the project (SWL, SWT, NAB, LRL) or plain Flow
# at a tailwater location (LRH, LRP, NAE), so plain Flow within the matching radius counts as the release.
CWMS_PARAMS = r".*\.(Flow|Flow-Total|Flow-Out|Flow-Res Out|Flow-Outflow|Stor|Stor-Res|Stor-Conservation|Elev|Elev-Pool|Elev-Headwater|Elev-Forebay|Elev-Lake)\..*"
LAKE_PARAMS = ("00054", "00062", "62614", "62615", "62616", "72275")
ACTIVE_SINCE = pd.Timestamp("2026-08-01", tz="UTC")  # CWMS catalog extents lag real data by ~2 weeks
DELAWARE_NYC = ("Cannonsville", "Pepacton", "Downsville", "Neversink")


def _cached_json(path: Path, fetch) -> object:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fetch()))
    return json.loads(path.read_text())


def _usgs_headers() -> dict:
    key = os.environ.get("API_DATA_GOV_KEY")
    return {"X-Api-Key": key} if key else {}


def _ogc_all(collection: str, params: dict) -> list[dict]:
    url, p, out = f"{OGC}/{collection}/items", {**params, "limit": 10000, "f": "json"}, []
    while url:
        r = requests.get(url, params=p, headers=_usgs_headers(), timeout=300)
        r.raise_for_status()
        j = r.json()
        out += j.get("features", [])
        url, p = next((ln["href"] for ln in j.get("links", []) if ln.get("rel") == "next"), None), None
    return out


# ------------------------------------------------------------------ inputs

def load_dams(cache: Path) -> gpd.GeoDataFrame:
    path = cache / "nid_nation.csv"
    if not path.exists():
        path.write_bytes(requests.get(NID_URL, timeout=900).content)
    raw = pd.read_csv(path, skiprows=1, low_memory=False)
    d = raw[raw["Other Structure ID"].isna()].copy()
    d["max_af"] = pd.to_numeric(d["NID Storage (Acre-Ft)"], errors="coerce").fillna(0.0)
    d["normal_af"] = pd.to_numeric(d["Normal Storage (Acre-Ft)"], errors="coerce").fillna(d["max_af"])
    owners = d["Federal Agency Owners"].fillna("")
    regulators = d["Federal Agency Involvement Regulatory"].fillna("")
    d["operator"] = np.select(
        [owners.str.contains("Corps of Engineers"), owners.str.contains("Reclamation"), owners.str.contains("Tennessee Valley"),
         owners != "", regulators.str.contains("Energy Regulatory")],
        ["USACE", "USBR", "TVA", "Other federal", "FERC licensee"], "State/local/private")
    d = d.dropna(subset=["Latitude", "Longitude"])
    cols = ["NID ID", "Dam Name", "State", "operator", "normal_af", "max_af", "Primary Purpose", "Latitude", "Longitude"]
    return gpd.GeoDataFrame(d[cols], geometry=gpd.points_from_xy(d["Longitude"], d["Latitude"]), crs="EPSG:4326")


def load_basins(cache: Path, sites: Iterable[str]) -> gpd.GeoDataFrame:
    path = cache / "basins_nldi.parquet"
    if path.exists():
        return gpd.read_parquet(path)
    s = requests.Session()

    def one(site: str):
        for _ in range(3):
            try:
                r = s.get(f"{NLDI}/nwissite/USGS-{site}/basin", params={"simplified": "true", "splitCatchment": "false"}, timeout=120)
                if r.ok:
                    return site, shape(r.json()["features"][0]["geometry"])
            except requests.RequestException:
                continue
        return site, None

    with ThreadPoolExecutor(6) as ex:
        got = [(k, g) for k, g in ex.map(one, sites) if g is not None]
    gdf = gpd.GeoDataFrame({"STAID": [k for k, _ in got]}, geometry=[g for _, g in got], crs="EPSG:4326")
    gdf.to_parquet(path)
    return gdf


def load_cwms(cache: Path) -> gpd.GeoDataFrame:
    """One row per CWMS base location with flags for the series it publishes."""
    headers = {"Accept": "application/json;version=2"}

    def catalog():
        out = {}
        for off in CWMS_OFFICES:
            entries, page = [], None
            while True:
                p = {"office": off, "like": CWMS_PARAMS, "page-size": 5000} | ({"page": page} if page else {})
                j = requests.get(f"{CWMS}/catalog/TIMESERIES", params=p, headers=headers, timeout=300).json()
                entries += j.get("entries", [])
                if not (page := j.get("next-page")):
                    break
            out[off] = entries
        return out

    def locations():
        return {off: requests.get(f"{CWMS}/catalog/LOCATIONS", params={"office": off, "page-size": 10000}, headers=headers, timeout=300).json().get("entries", [])
                for off in CWMS_OFFICES}

    cat = _cached_json(cache / "cwms_catalog.json", catalog)
    locs = _cached_json(cache / "cwms_locations.json", locations)
    coords = {}
    for off, entries in locs.items():
        for e in entries:
            if e.get("latitude") and e.get("longitude"):
                coords[(off, e["name"].split("-")[0])] = (e["latitude"], e["longitude"])
    rows: dict[tuple[str, str], dict] = {}
    for off, entries in cat.items():
        for e in entries:
            parts = e["name"].split(".")
            base, param, version = parts[0].split("-")[0], parts[1], parts[5]
            extents = e.get("extents") or []
            latest = max((pd.Timestamp(x["latest-time"]) for x in extents if x.get("latest-time")), default=None)
            earliest = min((pd.Timestamp(x["earliest-time"]) for x in extents if x.get("earliest-time")), default=None)
            if latest is None or latest < ACTIVE_SINCE:
                continue
            r = rows.setdefault((off, base), {"office": off, "location": base, "fcst_outflow": False, "obs_outflow": False,
                                              "storage": False, "elevation": False, "history_start": pd.NaT})
            forecast = any(k in version.lower() for k in ("fcst", "forecast", "projected"))
            rfc = any(k in version.upper() for k in ("RFC", "NWS", "WFO", "CHIPS"))
            if rfc and not forecast:
                continue  # RFC-sourced series (e.g. LRP "OHRFC") are forecasts even without the word
            if param.startswith("Flow"):
                if forecast and not rfc:
                    r["fcst_outflow"] = True
                elif not forecast:
                    r["obs_outflow"] = True
            elif param.startswith("Stor") and not forecast:
                r["storage"] = True
            elif param.startswith("Elev") and not forecast:
                r["elevation"] = True
            if not forecast and earliest is not None:
                r["history_start"] = min(filter(pd.notna, [r["history_start"], earliest]), default=earliest)
    df = pd.DataFrame([r | {"lat": coords.get(k, (None, None))[0], "lon": coords.get(k, (None, None))[1]} for k, r in rows.items()])
    df = df.dropna(subset=["lat", "lon"])
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


def load_usgs_lakes(cache: Path) -> gpd.GeoDataFrame:
    path = cache / "usgs_lake_series.parquet"
    if not path.exists():
        rows = []
        for pc in LAKE_PARAMS:
            for f in _ogc_all("time-series-metadata", {"parameter_code": pc}):
                p, geom = f["properties"], f.get("geometry") or {}
                lon, lat = (geom.get("coordinates") or [None, None])[:2]
                rows.append({"site": p["monitoring_location_id"], "pc": pc, "period": p.get("computation_period_identifier"),
                             "begin": p.get("begin_utc"), "end": p.get("end_utc"), "lat": lat, "lon": lon})
        pd.DataFrame(rows).to_parquet(path)
    df = pd.read_parquet(path).dropna(subset=["lat", "lon"])
    df["begin"], df["end"] = pd.to_datetime(df["begin"], utc=True, errors="coerce"), pd.to_datetime(df["end"], utc=True, errors="coerce")
    df["storage"] = df["pc"] == "00054"
    agg = df.groupby("site").agg(lat=("lat", "first"), lon=("lon", "first"), storage=("storage", "any"), begin=("begin", "min"),
                                 realtime=("end", lambda e: bool((e >= ACTIVE_SINCE).any())))
    rt = df[(df["period"] == "Points") & (df["end"] >= ACTIVE_SINCE)].groupby("site").size()
    agg["realtime_iv"] = agg.index.isin(rt.index)
    agg = agg.reset_index()
    return gpd.GeoDataFrame(agg, geometry=gpd.points_from_xy(agg["lon"], agg["lat"]), crs="EPSG:4326")


def load_safewaters(cache: Path) -> gpd.GeoDataFrame:
    def fetch():
        page = requests.get("https://www.safewaters.com/facility/fife-brook/", headers={"User-Agent": "Mozilla/5.0 (compatible; flowcast-research)"}, timeout=120).text
        return safewaters.facility_coords(page)

    coords = _cached_json(cache / "safewaters_coords.json", fetch)
    df = pd.DataFrame([{"slug": k, "lat": v[0], "lon": v[1]} for k, v in coords.items()])
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


def load_resops(cache: Path) -> gpd.GeoDataFrame:
    attrs, inv = cache / "resops_reservoir_attributes.csv", cache / "resops_time_series_inventory.csv"
    if not attrs.exists():
        with RemoteZip(RESOPS_ZIP) as z:
            attrs.write_bytes(z.read("ResOpsUS/attributes/reservoir_attributes.csv"))
            inv.write_bytes(z.read("ResOpsUS/attributes/time_series_inventory.csv"))
    a = pd.read_csv(attrs)
    i = pd.read_csv(inv, encoding="utf-8-sig")
    df = a.merge(i[["DAM_ID", "STORAGE", "OUTFLOW"]], on="DAM_ID", how="left")
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["LONG"], df["LAT"]), crs="EPSG:4326")


def active_discharge_sites(cache: Path) -> set[str]:
    path = cache / "usgs_q_points.parquet"
    if not path.exists():
        feats = _ogc_all("time-series-metadata", {"parameter_code": "00060", "computation_period_identifier": "Points",
                                                  "skipGeometry": "true", "properties": "monitoring_location_id,end_utc"})
        pd.DataFrame([(f["properties"]["monitoring_location_id"], f["properties"].get("end_utc")) for f in feats],
                     columns=["site", "end"]).to_parquet(path)
    df = pd.read_parquet(path)
    return set(df.loc[pd.to_datetime(df["end"], utc=True, errors="coerce") >= ACTIVE_SINCE, "site"].str.removeprefix("USGS-"))


def release_gauges(dams: gpd.GeoDataFrame, active: set[str], cache: Path, max_km: float = 15.0) -> pd.Series:
    """NID ID -> nearest active USGS discharge site downstream on the main stem within `max_km` (NLDI)."""
    path = cache / "release_gauges.json"
    known = json.loads(path.read_text()) if path.exists() else {}
    s = requests.Session()

    def one(row) -> tuple[str, str | None]:
        nid, lat, lon = row
        try:
            comid = s.get(f"{NLDI}/comid/position", params={"coords": f"POINT({lon} {lat})"}, timeout=60).json()["features"][0]["properties"]["comid"]
            feats = s.get(f"{NLDI}/comid/{comid}/navigation/DM/nwissite", params={"distance": max_km}, timeout=60).json().get("features", [])
        except (requests.RequestException, KeyError, IndexError, ValueError):
            return nid, None
        best = None
        for f in feats:
            site = f["properties"]["identifier"].removeprefix("USGS-")
            if site not in active:
                continue
            glon, glat = f["geometry"]["coordinates"][:2]
            km = float(np.hypot((glon - lon) * 111 * np.cos(np.radians(lat)), (glat - lat) * 111))
            if km <= max_km and (best is None or km < best[1]):
                best = (site, km)
        return nid, best[0] if best else ""

    todo = [(r["NID ID"], r["Latitude"], r["Longitude"]) for _, r in dams.iterrows() if r["NID ID"] not in known]
    with ThreadPoolExecutor(6) as ex:
        for nid, site in ex.map(one, todo):
            if site is not None:
                known[nid] = site
    path.write_text(json.dumps(known))
    return pd.Series(known)


# ------------------------------------------------------------------ analysis

def _near(dams: gpd.GeoDataFrame, pts: gpd.GeoDataFrame, meters: float) -> pd.DataFrame:
    a, b = dams.to_crs(EQUAL_AREA), pts.to_crs(EQUAL_AREA)
    j = gpd.sjoin_nearest(a, b, how="left", max_distance=meters, distance_col="_dist")
    return j[~j.index.duplicated()]


def dam_coverage(cache: Path, sites: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per basin dam with source flags, dam-basin pairs)."""
    dams = load_dams(cache)
    basins = load_basins(cache, sites)
    pairs = gpd.sjoin(dams, basins, predicate="within")[["NID ID", "STAID"]]
    d = dams[dams["NID ID"].isin(pairs["NID ID"]) & (dams["normal_af"] >= MIN_STORAGE_AF)].copy().reset_index(drop=True)

    cw = _near(d, load_cwms(cache), 3000)
    for col in ("fcst_outflow", "obs_outflow", "storage", "elevation"):
        d[f"cwms_{col}"] = cw[col].fillna(False).astype(bool).to_numpy()
    d["cwms_history_start"] = cw["history_start"].to_numpy()
    lk = _near(d, load_usgs_lakes(cache), 5000)
    d["usgs_lake_realtime"] = lk["realtime"].fillna(False).astype(bool).to_numpy()
    d["usgs_storage_realtime"] = (lk["realtime"].fillna(False) & lk["storage"].fillna(False)).astype(bool).to_numpy()
    d["usgs_lake_begin"] = lk["begin"].to_numpy()
    d["safewaters"] = _near(d, load_safewaters(cache), 3000)["slug"].notna().to_numpy()
    ro = _near(d, load_resops(cache), 3000)
    d["resops"] = ro["DAM_ID"].notna().to_numpy()
    d["resops_storage"] = (ro["STORAGE"] == 1).to_numpy()
    d["delaware_nyc"] = d["Dam Name"].str.contains("|".join(DELAWARE_NYC), case=False) & (d["State"] == "New York")
    gauges = release_gauges(d, active_discharge_sites(cache), cache)
    d["release_gauge"] = d["NID ID"].map(gauges).fillna("")
    d["schedule"] = d["cwms_fcst_outflow"] | d["safewaters"] | d["delaware_nyc"]
    d["observed_release"] = d["cwms_obs_outflow"] | (d["release_gauge"] != "") | d["safewaters"]
    d["storage_realtime"] = d["cwms_storage"] | d["cwms_elevation"] | d["usgs_lake_realtime"] | d["safewaters"] | d["delaware_nyc"]
    return d, pairs


def summarize(d: pd.DataFrame, pairs: pd.DataFrame) -> str:
    total = d["normal_af"].sum()

    def share(mask) -> str:
        return f"{mask.sum()} dams, {d.loc[mask, 'normal_af'].sum() / total:.0%} of storage"

    old = pd.Timestamp("2011-01-01", tz="UTC")
    hist_cwms = d["cwms_storage"] | d["cwms_elevation"]
    hist_cwms &= pd.to_datetime(d["cwms_history_start"], utc=True) <= old
    hist_usgs = d["usgs_lake_realtime"] & (pd.to_datetime(d["usgs_lake_begin"], utc=True) <= old)
    lines = [
        f"Dams >= {MIN_STORAGE_AF:,.0f} acre-ft inside {pairs['STAID'].nunique()} basin polygons: {len(d)} ({total / 1e6:.1f} M acre-ft)",
        "",
        "| Source | Dams | Share of storage |", "|---|---|---|",
    ]
    rows = [
        ("Published schedule: CWMS operator forecast outflow", d["cwms_fcst_outflow"]),
        ("Published schedule: Safe Waters (Brookfield)", d["safewaters"]),
        ("Published schedule: Delaware NYC reservoirs (ODRM/NYC DEP)", d["delaware_nyc"]),
        ("**Any published schedule**", d["schedule"]),
        ("Observed release: CWMS outflow", d["cwms_obs_outflow"]),
        ("Observed release: active USGS gauge <= 15 km downstream", d["release_gauge"] != ""),
        ("**Any observed release**", d["observed_release"]),
        ("Real-time storage or pool: CWMS", d["cwms_storage"] | d["cwms_elevation"]),
        ("Real-time storage or pool: USGS lake gauge", d["usgs_lake_realtime"]),
        ("**Any real-time storage or pool**", d["storage_realtime"]),
        ("Real-time storage/pool with history back to 2010 (CWMS or USGS)", hist_cwms | hist_usgs),
        ("History only: ResOpsUS (daily, ends 2020)", d["resops"]),
        ("None of the above", ~(d["schedule"] | d["observed_release"] | d["storage_realtime"] | d["resops"])),
    ]
    lines += [f"| {name} | {m.sum()} | {d.loc[m, 'normal_af'].sum() / total:.0%} |" for name, m in rows]
    lines += ["", "By operator (share of storage with any schedule / observed release / real-time storage):", "",
              "| Operator | Dams | Storage share | Schedule | Observed release | Real-time storage |", "|---|---|---|---|---|---|"]
    for op, g in d.groupby("operator"):
        st = g["normal_af"].sum()
        lines.append(f"| {op} | {len(g)} | {st / total:.0%} | {g.loc[g['schedule'], 'normal_af'].sum() / st:.0%} | "
                     f"{g.loc[g['observed_release'], 'normal_af'].sum() / st:.0%} | {g.loc[g['storage_realtime'], 'normal_af'].sum() / st:.0%} |")
    # Per basin: share of upstream storage covered, then how many basins clear 50%.
    m = pairs.merge(d[["NID ID", "normal_af", "schedule", "observed_release", "storage_realtime"]], on="NID ID")
    per = m.groupby("STAID").apply(lambda g: pd.Series({c: g.loc[g[c], "normal_af"].sum() / g["normal_af"].sum() for c in ("schedule", "observed_release", "storage_realtime")}), include_groups=False)
    lines += ["", f"Basins with at least one dam >= {MIN_STORAGE_AF:,.0f} acre-ft: {len(per)}. Basins where the source covers >= 50% of upstream storage:", "",
              f"- schedule: {(per['schedule'] >= 0.5).sum()}", f"- observed release: {(per['observed_release'] >= 0.5).sum()}",
              f"- real-time storage/pool: {(per['storage_realtime'] >= 0.5).sum()}"]
    return "\n".join(lines)
