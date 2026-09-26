"""Hydrologic response units (sub-basin x elevation band) with terrain radiation factors.

Artifacts (`HRUSet.save`), matching the plan's `sites/{id}/` layout:
- `hrus.parquet`: one row per HRU (ids, elevation band, area, centroid, slope/aspect, sky-view factor, forest fraction)
- `terrain_lut.npz`: direct-beam illumination table K[hru, azimuth, elevation]
- `hrus.geojson`: HRU polygons (lon/lat) for maps
- `meta.json`: build metadata
"""

import itertools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd
import requests
import shapely
from rasterio import features as rio_features
from rasterio.transform import Affine
from shapely import affinity
from shapely.geometry import mapping, shape

from . import terrain
from .dem import DEMGrid, fetch_dem, global_pixel_to_lonlat, lonlat_to_global_pixel

NLDI_BASIN_URL = "https://api.water.usgs.gov/nldi/linked-data/nwissite/{site}/basin"
BUILD_VERSION = 1
ASPECT_CLASSES = {1: ("",), 2: ("n", "s"), 4: ("n", "e", "s", "w")}


def fetch_nldi_basin(site: str) -> shapely.Geometry:
    """Drainage basin polygon for a USGS site (e.g. "USGS-01423000") from the NLDI."""
    site = site if site.startswith("USGS-") else f"USGS-{site}"
    resp = requests.get(NLDI_BASIN_URL.format(site=site), params={"simplified": "false", "splitCatchment": "false"}, timeout=120)
    resp.raise_for_status()
    return shapely.union_all([shape(f["geometry"]) for f in resp.json()["features"]])


@dataclass
class HRUSet:
    table: pd.DataFrame
    lut: np.ndarray
    geometry: dict | None = None
    meta: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.table)

    @property
    def ids(self) -> list[str]:
        return self.table["hru_id"].tolist()

    def reference_elevation(self) -> float:
        """Area-weighted mean elevation of all HRUs (the elevation basin-mean forcing is assumed to represent)."""
        w = self.table["area_km2"].to_numpy()
        return float(np.sum(w * self.table["elev_mean"].to_numpy()) / np.sum(w))

    def area_weights(self) -> np.ndarray:
        w = self.table["area_km2"].to_numpy(dtype=np.float64)
        return w / w.sum()

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        self.table.to_parquet(path / "hrus.parquet", index=False)
        np.savez_compressed(path / "terrain_lut.npz", lut=self.lut.astype(np.float32), azimuths=terrain.LUT_AZIMUTHS, elevations=terrain.LUT_ELEVATIONS)
        if self.geometry is not None:
            (path / "hrus.geojson").write_text(json.dumps(self.geometry))
        (path / "meta.json").write_text(json.dumps(self.meta, indent=2))
        return path

    @classmethod
    def load(cls, path: Path | str) -> "HRUSet":
        path = Path(path)
        table = pd.read_parquet(path / "hrus.parquet")
        with np.load(path / "terrain_lut.npz") as z:
            lut = z["lut"].astype(np.float64)
        geo = path / "hrus.geojson"
        geometry = json.loads(geo.read_text()) if geo.exists() else None
        meta_path = path / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return cls(table, lut, geometry, meta)

    @classmethod
    def concat(cls, sets: list["HRUSet"]) -> "HRUSet":
        table = pd.concat([s.table for s in sets], ignore_index=True)
        feats = [f for s in sets if s.geometry for f in s.geometry["features"]]
        geometry = {"type": "FeatureCollection", "features": feats} if feats else None
        return cls(table, np.concatenate([s.lut for s in sets]), geometry, {"parts": [s.meta for s in sets]})

    def subset(self, hru_ids: list[str]) -> "HRUSet":
        idx = [self.ids.index(h) for h in hru_ids]
        geometry = None
        if self.geometry is not None:
            bands = set(self.table.iloc[idx]["band_id"])
            geometry = {"type": "FeatureCollection", "features": [f for f in self.geometry["features"] if f["properties"]["band_id"] in bands]}
        return HRUSet(self.table.iloc[idx].reset_index(drop=True), self.lut[idx], geometry, dict(self.meta))

    def grid_weights(self, lat: np.ndarray, lon: np.ndarray, supersample: int = 3) -> pd.DataFrame:
        """Area weights of a regular lat/lon grid (e.g. AORC 1 km) for each elevation-band polygon.

        Returns rows (band_id, i_lat, j_lon, weight) with weights summing to 1 per band. Aspect classes of a band
        share its forcing (a 1 km grid cannot separate hillslopes). Uses `supersample`^2 sub-points per grid cell.
        """
        if self.geometry is None:
            raise ValueError("HRU geometry is required for grid weights")
        lat = np.asarray(lat)
        lon = np.asarray(lon)
        dlat = abs(lat[1] - lat[0])
        dlon = abs(lon[1] - lon[0])
        offs = (np.arange(supersample) + 0.5) / supersample - 0.5
        rows = []
        for feat in self.geometry["features"]:
            geom = shape(feat["geometry"])
            x0, y0, x1, y1 = geom.bounds
            jj = np.where((lon >= x0 - dlon) & (lon <= x1 + dlon))[0]
            ii = np.where((lat >= y0 - dlat) & (lat <= y1 + dlat))[0]
            if len(ii) == 0 or len(jj) == 0:
                continue
            LA, LO, OA, OO = np.meshgrid(lat[ii], lon[jj], offs * dlat, offs * dlon, indexing="ij")
            inside = shapely.contains_xy(geom, LO + OO, LA + OA).reshape(len(ii), len(jj), -1).mean(axis=2)
            si, sj = np.nonzero(inside)
            w = inside[si, sj]
            for a, b, c in zip(ii[si], jj[sj], w / w.sum()):
                rows.append((feat["properties"]["band_id"], int(a), int(b), float(c)))
        return pd.DataFrame(rows, columns=["band_id", "i_lat", "j_lon", "weight"])


def _buffered_bbox(geoms: list, buffer_km: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = shapely.union_all(geoms).bounds
    dlat = buffer_km / 111.0
    dlon = buffer_km / (111.0 * math.cos(math.radians((y0 + y1) / 2)))
    return x0 - dlon, y0 - dlat, x1 + dlon, y1 + dlat


def _to_pixel_space(geom, zoom: int):
    return shapely.transform(geom, lambda xy: np.column_stack(lonlat_to_global_pixel(xy[:, 0], xy[:, 1], zoom)))


def _to_lonlat(geom, zoom: int):
    return shapely.transform(geom, lambda xy: np.column_stack(global_pixel_to_lonlat(xy[:, 0], xy[:, 1], zoom)))


def _band_edges(elev: np.ndarray, n_bands: int) -> np.ndarray:
    edges = np.quantile(elev, np.linspace(0.0, 1.0, n_bands + 1))
    edges[0] -= 1e-3
    edges[-1] += 1e-3
    return edges


def _circular_mean_deg(angles_rad: np.ndarray, weights: np.ndarray) -> float:
    return float(np.degrees(np.arctan2(np.sum(weights * np.sin(angles_rad)), np.sum(weights * np.cos(angles_rad)))) % 360.0)


def build_hrus(
    subbasins: Mapping[str, shapely.Geometry],
    n_bands: int = 4,
    n_aspects: int = 2,
    band_edges: Mapping[str, list[float]] | list[float] | None = None,
    forest_frac: Mapping[str, float] | float = 0.0,
    zoom: int | None = None,
    n_dir: int = 16,
    buffer_km: float = 10.0,
    max_lut_cells: int = 150_000,
    with_geometry: bool = True,
    dem: DEMGrid | None = None,
) -> HRUSet:
    """Split each sub-basin into elevation bands x aspect classes and compute terrain radiation factors from a DEM.

    Bands are equal-area elevation quantiles per sub-basin unless `band_edges` gives explicit edges (m), either one
    list for all sub-basins or a mapping per sub-basin. Band 1 is the lowest. `n_aspects` = 2 splits each band into
    north- and south-facing hillslopes (4: N/E/S/W; 1: no split) so aspect-driven melt differences are resolved.
    HRU ids are "{subbasin}:b{band}{aspect}", e.g. "USGS-01423000:b2s".
    """
    ids = list(subbasins)
    geoms = [subbasins[s] for s in ids]
    dem = dem if dem is not None else fetch_dem(_buffered_bbox(geoms, buffer_km), zoom=zoom)
    elev = dem.elev
    pixel_m = dem.pixel_size_m()
    slope, aspect = terrain.slope_aspect(elev, pixel_m)
    hor = terrain.horizon_angles(elev, pixel_m, n_dir=n_dir)
    svf = terrain.sky_view_factor(hor, slope, aspect)
    cell_area_km2 = (pixel_m**2 / 1e6)[:, None] * np.ones_like(elev)

    aspect_class = _aspect_classes(elev, pixel_m, n_aspects)

    transform = Affine(1.0, 0.0, dem.px0, 0.0, 1.0, dem.py0)
    band_labels = np.full(elev.shape, -1, dtype=np.int32)
    band_ids: list[str] = []
    rows = []
    lon_c = dem.lon()
    lat_c = dem.lat()
    rng = np.random.default_rng(0)
    lut_cells: list[np.ndarray] = []
    lut_labels: list[np.ndarray] = []
    for sid, geom in zip(ids, geoms):
        mask = rio_features.rasterize(
            [(_to_pixel_space(geom, dem.zoom), 1)], out_shape=elev.shape, transform=transform, fill=0, all_touched=False, dtype="uint8"
        ).astype(bool)
        if not mask.any():
            mask = rio_features.rasterize(
                [(_to_pixel_space(geom, dem.zoom), 1)], out_shape=elev.shape, transform=transform, fill=0, all_touched=True, dtype="uint8"
            ).astype(bool)
        if not mask.any():
            raise ValueError(f"sub-basin {sid} does not overlap the DEM")
        sub_elev = elev[mask]
        if band_edges is None:
            edges = _band_edges(sub_elev, n_bands)
        else:
            edges = np.asarray(band_edges[sid] if isinstance(band_edges, Mapping) else band_edges, dtype=np.float64)
        band_of = np.clip(np.searchsorted(edges, elev, side="right") - 1, 0, len(edges) - 2)
        sub_area = float(cell_area_km2[mask].sum())
        ff = float(forest_frac[sid] if isinstance(forest_frac, Mapping) else forest_frac)
        for b, a in itertools.product(range(len(edges) - 1), range(n_aspects)):
            band_mask = mask & (band_of == b)
            if a == 0 and band_mask.any():
                band_labels[band_mask] = len(band_ids)
                band_ids.append(f"{sid}:b{b + 1}")
            m = band_mask & (aspect_class == a)
            if not m.any():
                continue
            k = len(rows)
            w = cell_area_km2[m]
            ii, jj = np.nonzero(m)
            rows.append(
                {
                    "hru_id": f"{sid}:b{b + 1}{ASPECT_CLASSES[n_aspects][a]}",
                    "subbasin_id": sid,
                    "band_id": f"{sid}:b{b + 1}",
                    "band": b + 1,
                    "aspect_class": ASPECT_CLASSES[n_aspects][a] or "all",
                    "elev_lo": float(edges[b]),
                    "elev_hi": float(edges[b + 1]),
                    "elev_mean": float(np.sum(w * elev[m]) / w.sum()),
                    "area_km2": float(w.sum()),
                    "frac_subbasin": float(w.sum() / sub_area),
                    "lat": float(np.sum(w * lat_c[ii]) / w.sum()),
                    "lon": float(np.sum(w * lon_c[jj]) / w.sum()),
                    "slope_deg": float(np.degrees(np.sum(w * slope[m]) / w.sum())),
                    "aspect_deg": _circular_mean_deg(aspect[m], w * np.sin(slope[m])),
                    "northness": float(np.sum(w * np.cos(aspect[m]) * np.sin(slope[m])) / w.sum()),
                    "eastness": float(np.sum(w * np.sin(aspect[m]) * np.sin(slope[m])) / w.sum()),
                    "svf": float(np.sum(w * svf[m]) / w.sum()),
                    "forest_frac": ff,
                }
            )
            flat_idx = np.flatnonzero(m)
            if len(flat_idx) > max_lut_cells:
                flat_idx = rng.choice(flat_idx, max_lut_cells, replace=False)
            lut_cells.append(flat_idx)
            lut_labels.append(np.full(len(flat_idx), k))

    table = pd.DataFrame(rows)
    table["frac_total"] = table["area_km2"] / table["area_km2"].sum()
    cells = np.concatenate(lut_cells)
    lut = terrain.illumination_lut(
        np.concatenate(lut_labels), len(table), slope.ravel()[cells], aspect.ravel()[cells], hor.reshape(n_dir, -1)[:, cells]
    )
    geometry = _band_geometry(band_labels, band_ids, table, dem, transform) if with_geometry else None
    meta = {
        "build_version": BUILD_VERSION,
        "dem": "AWS Terrain Tiles (terrarium)",
        "dem_zoom": dem.zoom,
        "pixel_m": float(np.median(pixel_m)),
        "n_dir": n_dir,
        "n_aspects": n_aspects,
        "subbasins": ids,
    }
    return HRUSet(table, lut, geometry, meta)


def _box_mean(a: np.ndarray, size: int) -> np.ndarray:
    pad = size // 2
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[size:, size:] - c[:-size, size:] - c[size:, :-size] + c[:-size, :-size]
    return s / (size * size)


def _aspect_classes(elev: np.ndarray, pixel_m: np.ndarray, n_aspects: int) -> np.ndarray:
    """Aspect class per pixel from a ~200 m smoothed DEM so classes follow hillslopes, not pixel noise."""
    if n_aspects == 1:
        return np.zeros(elev.shape, dtype=np.int8)
    size = max(3, int(round(200.0 / float(np.median(pixel_m)))) | 1)
    _, aspect = terrain.slope_aspect(_box_mean(elev.astype(np.float64), size), pixel_m)
    if n_aspects == 2:
        return (np.cos(aspect) < 0).astype(np.int8)  # 0 north-facing, 1 south-facing
    if n_aspects == 4:
        return (np.floor(np.mod(np.degrees(aspect) + 45.0, 360.0) / 90.0)).astype(np.int8)  # N, E, S, W
    raise ValueError("n_aspects must be 1, 2 or 4")


def _band_geometry(labels: np.ndarray, band_ids: list[str], table: pd.DataFrame, dem: DEMGrid, transform: Affine) -> dict:
    """Elevation-band polygons (aspect classes dissolved), keyed by `band_id`."""
    polys: dict[int, list] = {}
    for geom, value in rio_features.shapes(labels, mask=labels >= 0, transform=transform, connectivity=4):
        polys.setdefault(int(value), []).append(shape(geom))
    features = []
    tol = 1.5  # pixels
    for k, band_id in enumerate(band_ids):
        g = shapely.union_all(polys.get(k, [])).simplify(tol, preserve_topology=True)
        g = shapely.set_precision(_to_lonlat(g, dem.zoom), 1e-6)
        rows = table[table["band_id"] == band_id]
        w = rows["area_km2"].to_numpy()
        props = {
            "band_id": band_id,
            "subbasin_id": str(rows["subbasin_id"].iloc[0]),
            "band": int(rows["band"].iloc[0]),
            "elev_mean": round(float(np.sum(w * rows["elev_mean"].to_numpy()) / w.sum()), 1),
            "area_km2": round(float(w.sum()), 3),
            "hru_ids": rows["hru_id"].tolist(),
        }
        features.append({"type": "Feature", "geometry": json.loads(json.dumps(mapping(g))), "properties": props})
    return {"type": "FeatureCollection", "features": features}


def build_point_hrus(points: Mapping[str, tuple[float, float, float | None]], radius_m: float = 60.0, n_dir: int = 32) -> HRUSet:
    """Single-HRU sets around station points: {id: (lon, lat, elevation_m or None)}.

    The HRU is the DEM pixels within `radius_m`; a given station elevation replaces the DEM mean.
    """
    sets = []
    for pid, (lon, lat, z) in points.items():
        r_lat = max(radius_m, 45.0) / 111000.0
        circle = affinity.scale(shapely.Point(lon, lat).buffer(r_lat, 32), 1.0 / math.cos(math.radians(lat)), 1.0)
        hs = build_hrus({pid: circle}, n_bands=1, n_aspects=1, zoom=13, n_dir=n_dir, buffer_km=10.0, with_geometry=False)
        if z is not None and np.isfinite(z):
            hs.table.loc[:, "elev_mean"] = float(z)
        hs.table.loc[:, "hru_id"] = pid
        hs.table.loc[:, "band_id"] = pid
        sets.append(hs)
    return HRUSet.concat(sets)
