"""Name a gauge's basin from the USGS Watershed Boundary Dataset (WBD), for any site.

    python scripts/fetch_basin_name.py 01427510 [01011000 ...]

Reads the basin outline from ../landing/public/data/sites/<site>/geo.json and finds the WBD units overlapping it,
from the smallest (HUC-12) to the largest (HUC-6). The basin takes the name of the smallest unit covering at least
half of it, and the units at that level with at least 5% of the basin are listed as its parts. Shares are estimated
by sampling points inside the basin outline. Writes src/data/basin-<site>.json.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

WBD = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"
LEVELS = [(12, 6), (10, 5), (8, 4), (6, 3)]  # (HUC digits, WBD map layer)
ROOT = Path(__file__).resolve().parent.parent


def query(layer: int, bbox: list[float], digits: int) -> list[dict]:
    params = {
        "geometry": ",".join(map(str, bbox)),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": f"huc{digits},name,areasqkm",
        "returnGeometry": "true",
        "maxAllowableOffset": 0.002,
        "f": "json",
    }
    url = WBD.format(layer=layer) + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(url, timeout=60)).get("features", [])


def inside(x: float, y: float, rings: list) -> bool:
    """Even-odd test across all rings, so holes are handled whatever their winding."""
    hit = False
    for ring in rings:
        for (xi, yi), (xj, yj) in zip(ring, ring[-1:] + ring[:-1]):
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
    return hit


def basin_rings(geometry: dict) -> list:
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [ring for poly in polys for ring in poly]


def sample(rings: list, bbox: list[float], n: int = 70) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = bbox
    pts = [(x0 + (i + 0.5) * (x1 - x0) / n, y0 + (j + 0.5) * (y1 - y0) / n) for i in range(n) for j in range(n)]
    return [p for p in pts if inside(*p, rings)]


def name_basin(site: str) -> dict:
    geo = json.loads((ROOT.parent / "landing" / "public" / "data" / "sites" / site / "geo.json").read_text())
    rings = basin_rings(geo["basin"]["geometry"])
    bbox = geo["bounds"]
    pts = sample(rings, bbox)
    for digits, layer in LEVELS:
        units = []
        for f in query(layer, bbox, digits):
            ur = f["geometry"]["rings"]
            share = sum(inside(x, y, ur) for x, y in pts) / len(pts)
            if share > 0:
                a = f["attributes"]
                units.append({"huc": a[f"huc{digits}"], "name": a["name"], "share": round(share, 3), "area_km2": a["areasqkm"]})
        units.sort(key=lambda u: -u["share"])
        if units and units[0]["share"] >= 0.5:
            return {
                "site": site,
                "level": f"HUC-{digits}",
                "huc": units[0]["huc"],
                "name": units[0]["name"],
                "parts": [u for u in units if u["share"] >= 0.05],
                "source": "USGS Watershed Boundary Dataset",
            }
    raise SystemExit(f"{site}: no WBD unit covers half the basin")


def main(sites: list[str]) -> None:
    for site in sites:
        out = name_basin(site)
        path = ROOT / "src" / "data" / f"basin-{site}.json"
        path.write_text(json.dumps(out, indent=1))
        parts = ", ".join(f"{p['name']} {p['share']:.0%}" for p in out["parts"])
        print(f"{site}: {out['name']} ({out['level']} {out['huc']}); parts: {parts}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["01427510"])
