"""CAMELSH (Tran et al. 2025) attributes and basin boundaries.

Only the small archives are used (attributes, shapefiles, info.csv). Hourly targets come from the USGS
Water Data API instead of CAMELSH's own record, which has no observations for most Northeast gauges
(including every Upper Delaware gauge).
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import py7zr
import requests

log = logging.getLogger(__name__)

ZENODO = "https://zenodo.org/api/records/15066778/files/{name}/content"
FILES = ("info.csv", "attributes.7z", "shapefiles.7z")


def download(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = root / name
        if not path.exists():
            log.info("downloading CAMELSH %s", name)
            with requests.get(ZENODO.format(name=name), stream=True, timeout=600) as resp:
                resp.raise_for_status()
                tmp = path.with_suffix(path.suffix + ".part")
                with tmp.open("wb") as fh:
                    for block in resp.iter_content(1 << 20):
                        fh.write(block)
                tmp.replace(path)
        if name.endswith(".7z") and not (root / name.removesuffix(".7z")).exists():
            with py7zr.SevenZipFile(path) as archive:
                archive.extractall(root)
    return root


def attributes(root: Path) -> pd.DataFrame:
    """All GAGES-II and NLDAS climate attribute tables joined on STAID (index), as read."""
    frames = []
    for path in sorted((root / "attributes").glob("attributes_*.csv")):
        if path.name == "attributes_hydroATLAS.csv":
            continue
        df = pd.read_csv(path, dtype={"STAID": str}, low_memory=False).set_index("STAID")
        frames.append(df[[c for c in df.columns if all(c not in f.columns for f in frames)]])
    return pd.concat(frames, axis=1)


def boundaries(root: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(root / "shapefiles" / "CAMELSH_shapefile.shp")
    # Shipped without a .prj; coordinates are geographic NAD83, within a metre of WGS84.
    gdf = gdf.set_crs("EPSG:4326", allow_override=True)
    gdf["STAID"] = gdf["GAGE_ID"].astype(str).str.zfill(8)
    gdf["geometry"] = gdf.geometry.make_valid()
    return gdf.set_index("STAID")[["geometry"]]
