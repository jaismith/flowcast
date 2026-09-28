"""Regulation attributes from the National Inventory of Dams, below-dam gauge flags, and gauged-outflow discovery.

The same logic runs for every basin (rebuild plan §4 option E and §7 step 3):

* NID dams inside the basin, summarised as counts, storage relative to mean annual runoff (degree of
  regulation), purpose mix and how much of the basin sits behind the largest dam.
* A gauge is *below a dam* when a dam with meaningful storage sits close upstream and controls at least
  half of the gauge's drainage area.
* For each basin, upstream gauges that are themselves below dams become its optional *gauged outflow*
  input. Only the most downstream of any nested chain is kept, so flows are not double-counted.
"""

import logging
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

NID_URL = "https://nid.sec.usace.army.mil/api/nation/csv"
ACRE_FT_M3 = 1233.48184
SQMI_KM2 = 2.58998811
EQUAL_AREA = "EPSG:5070"

# NID "major dam" (as used by GAGES-II): >= 50 ft high, or >= 5,000 acre-ft normal, or >= 25,000 acre-ft max storage.
MAJOR_HEIGHT_FT = 50.0
MAJOR_NORMAL_AF = 5_000.0
MAJOR_MAX_AF = 25_000.0

BELOW_DAM_MIN_STORAGE_AF = 1_000.0
BELOW_DAM_MIN_DRAINAGE_FRAC = 0.5
BELOW_DAM_MAX_DIST_KM = 30.0

PURPOSES = {
    "flood": ("Flood Risk Reduction", "Debris Control"),
    "water_supply": ("Water Supply",),
    "hydro": ("Hydroelectric",),
    "recreation": ("Recreation", "Fish and Wildlife Pond", "Fire Protection, Stock, Or Small Fish Pond"),
    "navigation": ("Navigation",),
    "irrigation": ("Irrigation",),
}


def download_nid(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(NID_URL, timeout=600)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    return path


def load_nid(path: Path) -> gpd.GeoDataFrame:
    """One row per dam.

    Associated structures (dikes, levees, spillway sections: `Other Structure ID` set) share their parent's NID ID
    and often repeat its storage with no drainage area, so only the main structure is kept. Rows without an
    NID ID are distinct dams and get a synthetic ID.
    """
    raw = pd.read_csv(path, skiprows=1, low_memory=False)
    nid_id = raw["NID ID"].astype("string").str.strip()
    df = pd.DataFrame(
        {
            "nid_id": nid_id.fillna("row" + pd.Series(raw.index, index=raw.index).astype("string")),
            "associated": raw["Other Structure ID"].notna(),
            "name": raw["Dam Name"],
            "primary_purpose": raw["Primary Purpose"].fillna("Other"),
            "height_ft": pd.to_numeric(raw["NID Height (Ft)"], errors="coerce"),
            "year_completed": pd.to_numeric(raw["Year Completed"], errors="coerce"),
            "nid_storage_af": pd.to_numeric(raw["NID Storage (Acre-Ft)"], errors="coerce").fillna(0.0),
            "max_storage_af": pd.to_numeric(raw["Max Storage (Acre-Ft)"], errors="coerce"),
            "normal_storage_af": pd.to_numeric(raw["Normal Storage (Acre-Ft)"], errors="coerce"),
            "drainage_km2": pd.to_numeric(raw["Drainage Area (Sq Miles)"], errors="coerce") * SQMI_KM2,
            "lat": pd.to_numeric(raw["Latitude"], errors="coerce"),
            "lon": pd.to_numeric(raw["Longitude"], errors="coerce"),
        }
    ).dropna(subset=["lat", "lon"])
    df = df.sort_values(["associated", "nid_storage_af"], ascending=[True, False]).drop_duplicates("nid_id").sort_index()
    df["normal_storage_af"] = df["normal_storage_af"].fillna(df["nid_storage_af"])
    df["major"] = (
        (df["height_ft"] >= MAJOR_HEIGHT_FT)
        | (df["normal_storage_af"] >= MAJOR_NORMAL_AF)
        | (df["max_storage_af"].fillna(df["nid_storage_af"]) >= MAJOR_MAX_AF)
    )
    df["purpose_group"] = "other"
    for group, names in PURPOSES.items():
        df.loc[df["primary_purpose"].isin(names), "purpose_group"] = group
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


OUTLET_RING_M = 3_000.0
GAUGE_RING_M = 5_000.0
RING_DRAINAGE_FRAC = (0.5, 1.1)
ON_NETWORK_M = 500.0


def _matching_near(known: gpd.GeoDataFrame, staid: np.ndarray, geoms: gpd.GeoSeries, distance: float, area_km2: pd.Series) -> gpd.GeoDataFrame:
    src_idx, dam_idx = known.sindex.query(geoms, predicate="dwithin", distance=distance)
    near = known.iloc[dam_idx].copy()
    near["STAID"] = staid[src_idx]
    frac = near["drainage_km2"].to_numpy() / area_km2.reindex(near["STAID"]).to_numpy()
    return near[(frac >= RING_DRAINAGE_FRAC[0]) & (frac <= RING_DRAINAGE_FRAC[1])]


def dams_in_basins(
    dams: gpd.GeoDataFrame,
    basins: gpd.GeoDataFrame,
    area_km2: pd.Series,
    gauges: gpd.GeoSeries | None = None,
    networks: gpd.GeoSeries | None = None,
) -> gpd.GeoDataFrame:
    """Dam rows repeated per containing basin, with `STAID` of the basin.

    GAGES-II boundaries are coarse and NID points can sit a kilometre or two off, so a large dam right at
    the outlet often falls just outside its gauge's polygon (Cannonsville is 1.3 km outside 01425000's;
    Beltzville is 0.8 km from 01449800's gauge but 3 km outside its polygon). Dams within a ring around the
    polygon, or around the gauge point (`gauges`, indexed by STAID), are admitted only if their NID drainage
    area matches the basin's. Where a basin's NHDPlus upstream flowlines are given (`networks`, indexed by
    STAID, any CRS), those ring dams must also lie on them, which rejects dams just downstream or on a
    neighbouring river.
    """
    ea = basins.reset_index()[["STAID", "geometry"]].to_crs(EQUAL_AREA)
    pts = dams.to_crs(EQUAL_AREA)
    inside = gpd.sjoin(pts, ea, predicate="within", how="inner").drop(columns="index_right")
    known = pts[pts["drainage_km2"].notna()]
    near = [_matching_near(known, ea["STAID"].to_numpy(), ea.geometry, OUTLET_RING_M, area_km2)]
    if gauges is not None:
        gp = gauges[gauges.index.isin(ea["STAID"])].to_crs(EQUAL_AREA)
        near.append(_matching_near(known, gp.index.to_numpy(), gp.geometry, GAUGE_RING_M, area_km2))
    ring = pd.concat(near).drop_duplicates(["nid_id", "STAID"])
    ring = ring[~ring.set_index(["nid_id", "STAID"]).index.isin(inside.set_index(["nid_id", "STAID"]).index)]
    inside["ring"], ring["ring"] = False, True
    if networks is not None and len(ring):
        net = networks.to_crs(EQUAL_AREA)
        has = ring["STAID"].isin(net.index).to_numpy()
        dist = np.full(len(ring), 0.0)
        dist[has] = ring.geometry[has].distance(gpd.GeoSeries(net.loc[ring["STAID"][has]].to_numpy(), index=ring.index[has], crs=EQUAL_AREA)).to_numpy()
        ring = ring[dist <= ON_NETWORK_M]
    joined = pd.concat([inside, ring]).drop_duplicates(["nid_id", "STAID"])
    return gpd.GeoDataFrame(joined, geometry="geometry", crs=EQUAL_AREA).to_crs("EPSG:4326")


def _dam_distance_km(dams: pd.DataFrame, gauge_lat: float, gauge_lon: float) -> np.ndarray:
    lat1, lon1 = np.radians(dams["lat"].to_numpy()), np.radians(dams["lon"].to_numpy())
    lat2, lon2 = np.radians(gauge_lat), np.radians(gauge_lon)
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def below_dam(dams: pd.DataFrame, area_km2: float, gauge_lat: float, gauge_lon: float) -> dict[str, float]:
    """Is this gauge directly below a dam? `dams` are the NID dams inside the gauge's basin."""
    out = {"below_dam": 0.0, "below_dam_drainage_frac": 0.0, "below_dam_dist_km": np.nan, "below_dam_storage_af": 0.0, "below_dam_nid_id": ""}
    if dams.empty:
        return out
    dist = _dam_distance_km(dams, gauge_lat, gauge_lon)
    frac = (dams["drainage_km2"] / area_km2).clip(upper=1.0).to_numpy()
    big = (dams["normal_storage_af"].to_numpy() >= BELOW_DAM_MIN_STORAGE_AF) | dams["major"].to_numpy()
    hit = big & (np.nan_to_num(frac) >= BELOW_DAM_MIN_DRAINAGE_FRAC) & (dist <= BELOW_DAM_MAX_DIST_KM)
    if hit.any():
        i = np.flatnonzero(hit)[np.argmin(dist[hit])]
        out.update(
            below_dam=1.0,
            below_dam_drainage_frac=float(frac[i]),
            below_dam_dist_km=float(dist[i]),
            below_dam_storage_af=float(dams["normal_storage_af"].iloc[i]),
            below_dam_nid_id=str(dams["nid_id"].iloc[i]),
        )
    return out


def basin_attributes(dams: pd.DataFrame, area_km2: float, runoff_mm: float, gauge_lat: float, gauge_lon: float) -> dict[str, float]:
    """NID summary for one basin. `runoff_mm` is mean annual runoff (GAGES-II RUNAVE7100)."""
    area_m2 = area_km2 * 1e6
    runoff_m3 = max(runoff_mm, 1.0) / 1000.0 * area_m2
    normal = dams["normal_storage_af"].sum() * ACRE_FT_M3
    maximum = dams["nid_storage_af"].sum() * ACRE_FT_M3
    major = dams[dams["major"]]
    out: dict[str, float] = {
        "nid_n_dams": float(len(dams)),
        "nid_n_major": float(len(major)),
        "nid_dam_density_per_1000km2": len(dams) / area_km2 * 1000.0,
        "nid_normal_storage_mm": normal / area_m2 * 1000.0,
        "nid_max_storage_mm": maximum / area_m2 * 1000.0,
        "nid_dor_normal": normal / runoff_m3,
        "nid_dor_max": maximum / runoff_m3,
        "nid_major_dor_normal": major["normal_storage_af"].sum() * ACRE_FT_M3 / runoff_m3,
    }
    total = dams["nid_storage_af"].sum()
    for group in [*PURPOSES, "other"]:
        share = dams.loc[dams["purpose_group"] == group, "nid_storage_af"].sum()
        out[f"nid_storage_frac_{group}"] = share / total if total > 0 else 0.0
    if len(dams):
        frac = (dams["drainage_km2"] / area_km2).clip(upper=1.0)
        largest = dams.loc[dams["nid_storage_af"].idxmax()]
        out.update(
            nid_largest_storage_frac=largest["nid_storage_af"] / total if total > 0 else 0.0,
            nid_largest_dor=largest["normal_storage_af"] * ACRE_FT_M3 / runoff_m3,
            nid_largest_drainage_frac=float(np.nan_to_num(min(largest["drainage_km2"] / area_km2, 1.0))),
            nid_largest_dist_km=float(_dam_distance_km(dams.loc[[largest.name]], gauge_lat, gauge_lon)[0]),
            nid_max_drainage_frac=float(np.nan_to_num(frac.max())),
            nid_storage_weighted_year=float(np.average(dams["year_completed"].fillna(1950), weights=dams["nid_storage_af"] + 1e-9)),
        )
    else:
        out.update(
            nid_largest_storage_frac=0.0,
            nid_largest_dor=0.0,
            nid_largest_drainage_frac=0.0,
            nid_largest_dist_km=np.nan,
            nid_max_drainage_frac=0.0,
            nid_storage_weighted_year=np.nan,
        )
    out.update(below_dam(dams, area_km2, gauge_lat, gauge_lon))
    return out


def upstream_gauges(basin: str, polygons: gpd.GeoDataFrame, areas: pd.Series, min_overlap: float = 0.9) -> list[str]:
    """Gauges whose basins lie (almost) entirely inside `basin`'s, excluding itself. `polygons` in an equal-area CRS."""
    geom = polygons.geometry.loc[basin]
    cand = polygons.iloc[polygons.sindex.query(geom, predicate="intersects")]
    cand = cand[(cand.index != basin) & (areas.loc[cand.index] < 0.98 * areas.loc[basin])]
    if cand.empty:
        return []
    overlap = cand.geometry.intersection(geom).area / cand.geometry.area
    return overlap[overlap >= min_overlap].index.tolist()


def outflow_gauges(upstream: list[str], table: pd.DataFrame, polygons: gpd.GeoDataFrame, areas: pd.Series, min_overlap: float = 0.9) -> list[str]:
    """Pick one release gauge per dam, then drop release gauges that sit upstream of another dam's.

    Per dam, the gauge whose drainage best matches the dam's (the one immediately below it) wins. If dam A's
    gauge lies inside dam B's gauge basin, B's release already contains A's water, so A's gauge is dropped.
    """
    below = table.loc[upstream]
    below = below[below["below_dam"] > 0]
    per_dam = below.sort_values("below_dam_drainage_frac", ascending=False).groupby("below_dam_nid_id").head(1)
    keep: list[str] = []
    for g in sorted(per_dam.index, key=lambda s: -areas.loc[s]):
        geom = polygons.geometry.loc[g]
        if not any(geom.intersection(polygons.geometry.loc[k]).area / geom.area >= min_overlap for k in keep):
            keep.append(g)
    return sorted(keep)


OUTFLOW_MIN_RECORD_YEARS = 5.0
OUTFLOW_BEGIN_BY = pd.Timestamp("2019-10-01", tz="UTC")


def long_record(sites: list[str], inv_q: pd.DataFrame, camelsh_info: pd.DataFrame, now: pd.Timestamp) -> list[str]:
    """Sites with at least 5 years of hourly discharge since 2000, some of it inside the training years.

    `gauged_outflow_mm_h` is NaN whenever any of a basin's outflow gauges is missing, so a gauge whose record
    starts after the training years would blank the input for all of trainval. Record length counts CAMELSH
    (the target source before 2024) and the USGS API from 2024, or the API alone for sites without CAMELSH.
    """
    years = [str(y) for y in range(2000, 2024)]
    info = camelsh_info.set_index("STAID")[years].reindex(sites).fillna(0)
    cam_h = info.sum(axis=1)
    cam_first = info.gt(0).idxmax(axis=1).where(cam_h > 0)
    inv = inv_q.set_index("site").reindex(sites)
    api_from = inv["begin"].clip(lower=pd.Timestamp("2000-01-01", tz="UTC"))
    api_from = api_from.where(cam_h == 0, api_from.clip(lower=pd.Timestamp("2024-01-01", tz="UTC")))
    api_years = ((inv["end"].clip(upper=now) - api_from).dt.days / 365.25).clip(lower=0).fillna(0)
    record = cam_h / 8766.0 + api_years
    start = pd.to_datetime(cam_first.astype("string") + "-01-01", utc=True).fillna(inv["begin"])
    ok = (record >= OUTFLOW_MIN_RECORD_YEARS) & (start <= OUTFLOW_BEGIN_BY)
    return [s for s in sites if ok[s]]


def regulation_table(
    selected: list[str],
    candidates: list[str],
    attrs: pd.DataFrame,
    polygons: gpd.GeoDataFrame,
    dams: gpd.GeoDataFrame,
    networks: Callable[[list[str]], gpd.GeoSeries] | None = None,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Per-basin NID attributes plus gauged-outflow attributes, and the outflow gauges for each basin.

    `candidates` are gauges (with polygons and IV discharge) that may serve as gauged outflows. `networks`
    returns NHDPlus upstream flowlines for the given gauges (see `dams_in_basins`); it is only asked for
    gauges that admit a dam through an outlet or gauge ring.
    """
    gauges = sorted(set(selected) | set(candidates))
    areas = attrs.loc[gauges, "DRAIN_SQKM"].astype(float)
    points = gpd.GeoSeries(gpd.points_from_xy(attrs.loc[gauges, "LNG_GAGE"], attrs.loc[gauges, "LAT_GAGE"]), index=gauges, crs="EPSG:4326")
    in_basin = dams_in_basins(dams, polygons.loc[gauges], areas, points)
    if networks is not None:
        ringed = sorted(in_basin.loc[in_basin["ring"], "STAID"].unique())
        in_basin = dams_in_basins(dams, polygons.loc[gauges], areas, points, networks(ringed))
    by_basin = {k: v for k, v in in_basin.groupby("STAID")}
    empty = in_basin.iloc[0:0]
    ea = polygons.loc[gauges].to_crs(EQUAL_AREA)

    rows = {}
    for g in gauges:
        a = attrs.loc[g]
        rows[g] = basin_attributes(by_basin.get(g, empty), float(a["DRAIN_SQKM"]), float(a.get("RUNAVE7100", np.nan) or 0.0), float(a["LAT_GAGE"]), float(a["LNG_GAGE"]))
    table = pd.DataFrame.from_dict(rows, orient="index")

    below = set(table.index[table["below_dam"] > 0]) & set(candidates)
    outflows: dict[str, list[str]] = {}
    extra = []
    for b in selected:
        up = outflow_gauges([g for g in upstream_gauges(b, ea, areas) if g in below], table, ea, areas)
        outflows[b] = up
        area_b = areas.loc[b]
        behind = sum(areas.loc[g] for g in up)
        dams_b = by_basin.get(b, empty)
        behind_gauges = set().union(*(set(by_basin[g]["nid_id"]) for g in up if g in by_basin)) if up else set()
        # A below-dam gauge observes its own dam's releases through its lagged flow.
        if table.loc[b, "below_dam"] > 0:
            behind_gauges.add(table.loc[b, "below_dam_nid_id"])
        gauged = dams_b["nid_id"].isin(behind_gauges)
        total = dams_b["nid_storage_af"].sum()
        runoff_m3 = max(float(attrs.loc[b].get("RUNAVE7100", 0) or 0), 1.0) / 1000.0 * area_b * 1e6
        ungauged = dams_b.loc[~gauged, "normal_storage_af"].sum() * ACRE_FT_M3
        extra.append(
            {
                "STAID": b,
                "gauged_outflow_n": float(len(up)),
                "gauged_outflow_area_frac": min(behind / area_b, 1.0),
                "gauged_outflow_storage_frac": dams_b.loc[gauged, "nid_storage_af"].sum() / total if total > 0 else 0.0,
                "ungauged_dor_normal": ungauged / runoff_m3,
                "regulation_partly_observed": float(ungauged / runoff_m3 >= 0.1),
            }
        )
    table = table.loc[selected].join(pd.DataFrame(extra).set_index("STAID"))
    return table, outflows
