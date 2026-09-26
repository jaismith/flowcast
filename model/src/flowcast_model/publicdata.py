"""Small public smoke-test cube, used until the real training dataset is available.

* Discharge: USGS hourly (top-of-hour instantaneous values) from the Water Data API via `flowcast_pipeline`,
  converted to specific discharge `qobs_mm_h` with the USGS drainage area.
* Forcing: Open-Meteo historical API (`era5_seamless`: ERA5-Land where available, ERA5 elsewhere; CC BY 4.0,
  free for non-commercial use) at the NLDI basin polygon centroid. Reanalysis is not a forecast, so models trained
  and hindcast on it are `perfect_forcing` runs.
* Period: WY2001-WY2022 only. Nothing from the frozen test years (WY2023+) is downloaded.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from flowcast_pipeline.obs import hourly_observations
from flowcast_pipeline.usgs import WaterDataClient

from .cube import FROZEN_TEST_START, write_cube

log = logging.getLogger(__name__)

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite"
HOURLY_VARIABLES = {
    "precipitation": "precip_mm_h",
    "temperature_2m": "t2m_c",
    "dew_point_2m": "td2m_c",
    "shortwave_radiation": "swdown_w_m2",
    "surface_pressure": "psurf_hpa",
    "wind_speed_10m": "wind_km_h",
    "et0_fao_evapotranspiration": "pet_mm_h",
    "snow_depth": "snow_depth_m",
}
SQMI_TO_KM2 = 2.589988110336
# 1 mm/h over 1 km2 = 1e3 m3 / 3600 s
MM_H_KM2_TO_M3_S = 1.0 / 3.6
M3_S_TO_CFS = 35.314666721

# Upper Delaware / Catskills gauges with hourly records back to at least 2007: a mix of unregulated headwaters,
# the two below-dam gauges and regulated mainstem sites, including the Callicoon demo site.
DEFAULT_BASINS = [
    "01413500",  # EB Delaware at Margaretville (unregulated)
    "01414500",  # Mill Brook near Dunraven (unregulated)
    "01420500",  # Beaver Kill at Cooks Falls (unregulated)
    "01421000",  # EB Delaware at Fishs Eddy (regulated, Pepacton)
    "01423000",  # WB Delaware at Walton (unregulated)
    "01425000",  # WB Delaware at Stilesville (below Cannonsville)
    "01426500",  # WB Delaware at Hale Eddy (regulated)
    "01427510",  # Delaware at Callicoon (regulated, demo site)
    "01435000",  # Neversink near Claryville (unregulated)
    "01362500",  # Esopus Creek at Coldbrook (unregulated)
    "01350000",  # Schoharie Creek at Prattsville (unregulated)
    "01365000",  # Rondout Creek near Lowes Corners (unregulated)
]


def polygon_centroid(coords: list) -> tuple[float, float]:
    """Area-weighted centroid (lon, lat) of a GeoJSON polygon's outer ring (planar; fine for a basin)."""
    ring = np.asarray(coords[0], float)
    x, y = ring[:, 0], ring[:, 1]
    cross = x[:-1] * y[1:] - x[1:] * y[:-1]
    area = cross.sum() / 2.0
    cx = ((x[:-1] + x[1:]) * cross).sum() / (6.0 * area)
    cy = ((y[:-1] + y[1:]) * cross).sum() / (6.0 * area)
    return float(cx), float(cy)


def basin_geometry(gauge: str, session: requests.Session) -> dict:
    site = session.get(f"{NLDI}/USGS-{gauge}", timeout=60).json()["features"][0]
    basin = session.get(f"{NLDI}/USGS-{gauge}/basin", params={"simplified": "true"}, timeout=120).json()["features"][0]["geometry"]
    polys = [basin["coordinates"]] if basin["type"] == "Polygon" else basin["coordinates"]
    largest = max(polys, key=lambda p: len(p[0]))
    lon, lat = polygon_centroid(largest)
    return {"lon": lon, "lat": lat, "comid": int(site["properties"]["comid"])}


def open_meteo_hourly(lat: float, lon: float, start: str, end: str, cache: Path, session: requests.Session) -> pd.DataFrame:
    path = cache / f"openmeteo_{lat:.4f}_{lon:.4f}_{start}_{end}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(HOURLY_VARIABLES),
        "models": "era5_seamless",
        "timezone": "GMT",
    }
    for attempt in range(8):
        resp = session.get(OPEN_METEO_ARCHIVE, params=params, timeout=600)
        if resp.status_code == 429:
            wait = 120 * (attempt + 1)
            log.warning("Open-Meteo rate limit (%s); sleeping %ds", resp.text[:120], wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        raise RuntimeError("Open-Meteo kept rate-limiting")
    payload = resp.json()
    hourly = payload["hourly"]
    df = pd.DataFrame({new: hourly[old] for old, new in HOURLY_VARIABLES.items()}, index=pd.to_datetime(hourly["time"]))
    df.index.name = "date"
    df.attrs["elevation"] = payload.get("elevation")
    df = df.astype(np.float32)
    df["elevation_m"] = np.float32(payload.get("elevation") or np.nan)
    df.to_parquet(path)
    return df


def build_public_cube(out: str | Path, basins: list[str] | None = None, start: str = "2000-10-01", end: str = "2022-09-30", cache_dir: str | Path | None = None, pause_s: float = 20.0) -> Path:
    if pd.Timestamp(end) + pd.Timedelta(hours=23) >= FROZEN_TEST_START:
        raise ValueError("the public smoke cube stops before the frozen test years")
    basins = basins or DEFAULT_BASINS
    cache = Path(cache_dir or Path.home() / ".cache" / "flowcast" / "public-cube")
    cache.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    client = WaterDataClient()
    dynamic: dict[str, pd.DataFrame] = {}
    static_rows = []
    for gauge in basins:
        geo_path = cache / f"geo_{gauge}.json"
        if geo_path.exists():
            geo = json.loads(geo_path.read_text())
        else:
            geo = basin_geometry(gauge, session)
            meta = client.monitoring_location(gauge)
            geo["area_km2"] = float(meta["drainage_area"]) * SQMI_TO_KM2
            geo_path.write_text(json.dumps(geo))
        fresh = not any(cache.glob(f"openmeteo_{geo['lat']:.4f}_{geo['lon']:.4f}_{start}_{end}.parquet"))
        forcing = open_meteo_hourly(geo["lat"], geo["lon"], start, end, cache, session)
        if fresh:
            time.sleep(pause_s)
        q = hourly_observations(client, gauge, "discharge", start, f"{end}T23:59")
        q.index = q.index.tz_convert("UTC").tz_localize(None)
        q = q[~q.index.duplicated()]
        qmm = (q / M3_S_TO_CFS / MM_H_KM2_TO_M3_S / geo["area_km2"]).astype(np.float32)
        qmm[qmm < 0] = np.nan
        frame = forcing.drop(columns=["elevation_m"]).copy()
        frame["qobs_mm_h"] = qmm.reindex(frame.index)
        frame = frame[: pd.Timestamp(end) + pd.Timedelta(hours=23)]
        dynamic[gauge] = frame
        train = frame[: "2019-09-30"]
        static_rows.append(
            {
                "basin": gauge,
                "area_km2": geo["area_km2"],
                "log_area": float(np.log10(geo["area_km2"])),
                "lat": geo["lat"],
                "lon": geo["lon"],
                "elevation_m": float(forcing["elevation_m"].iloc[0]),
                "p_mean": float(train["precip_mm_h"].mean() * 24),
                "pet_mean": float(train["pet_mm_h"].mean() * 24),
                "t_mean": float(train["t2m_c"].mean()),
                "nwm_feature_id": float(geo["comid"]),
            }
        )
        log.info("%s: %d forcing hours, %d discharge hours", gauge, len(frame), int(frame["qobs_mm_h"].notna().sum()))
    static = pd.DataFrame(static_rows).set_index("basin")
    static["aridity"] = static["pet_mean"] / static["p_mean"]
    attrs = {
        "flowcast_schema": "flowcast-training-cube",
        "name": "public-smoke-openmeteo-usgs",
        "description": "Public smoke-test cube: USGS hourly discharge + Open-Meteo era5_seamless forcing at basin centroids, WY2001-WY2022",
        "target_unit": "mm/h",
        "area_attribute": "area_km2",
        "run_type": "perfect_forcing",
        "licenses": "USGS public domain; Open-Meteo/ERA5 CC BY 4.0",
    }
    out = Path(out)
    write_cube(out, dynamic, static, attrs)
    return out
