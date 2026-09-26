"""Gridded forcing products: where they live, how to read one block x tile, and unit conversion.

Every product maps onto the same output variables so the model sees one feature vocabulary with each
product as an optional, masked input (rebuild plan §1.2). Variables a product lacks are omitted.

| output            | units  | AORC | HRRR (an/fc) | MRMS | GEFS |
|-------------------|--------|------|--------------|------|------|
| precip_mm_h       | mm/h   | x    | x            | x    | x    |
| temp_2m_c         | degC   | x    | x            |      | x    |
| dewpoint_2m_c     | degC   | x*   | x            |      | x*   |
| pressure_kpa      | kPa    | x    | x            |      | x    |
| wind_speed_10m    | m/s    | x*   | x*           |      | x*   |
| sw_down_wm2       | W/m2   | x    | x            |      | x    |
| lw_down_wm2       | W/m2   | x    | x            |      | x    |
| spfh_2m_gkg       | g/kg   | x    |              |      |      |

(*) derived per grid cell before averaging: dewpoint from specific humidity + pressure (AORC) or
temperature + relative humidity (GEFS); wind speed from u/v, so basin means aren't damped by averaging vectors.

Time conventions: AORC and MRMS precipitation are hour-ending accumulations; HRRR and GEFS precipitation
and GEFS radiation are means over the step ending at the valid time (1 h HRRR, 3 h GEFS to 240 h).
"""

from dataclasses import dataclass, field, replace
from functools import cache

import icechunk
import numpy as np
import zarr

from . import grids

OUTPUTS = ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2", "spfh_2m_gkg")


def dewpoint_from_q(q_kgkg: np.ndarray, p_pa: np.ndarray) -> np.ndarray:
    e = np.maximum(q_kgkg * p_pa / (0.622 + 0.378 * q_kgkg), 1e-3) / 100.0  # hPa
    ln = np.log(e / 6.112)
    return 243.5 * ln / (17.67 - ln)


def dewpoint_from_rh(t_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    ln = np.log(np.clip(rh_pct, 1.0, 100.0) / 100.0) + 17.625 * t_c / (243.04 + t_c)
    return 243.04 * ln / (17.625 - ln)


def aorc_convert(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        "precip_mm_h": raw["APCP_surface"],
        "temp_2m_c": raw["TMP_2maboveground"] - 273.15,
        "dewpoint_2m_c": dewpoint_from_q(raw["SPFH_2maboveground"], raw["PRES_surface"]),
        "pressure_kpa": raw["PRES_surface"] / 1000.0,
        "wind_speed_10m": np.hypot(raw["UGRD_10maboveground"], raw["VGRD_10maboveground"]),
        "sw_down_wm2": raw["DSWRF_surface"],
        "lw_down_wm2": raw["DLWRF_surface"],
        "spfh_2m_gkg": raw["SPFH_2maboveground"] * 1000.0,
    }


def hrrr_convert(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        "precip_mm_h": raw["precipitation_surface"] * 3600.0,
        "temp_2m_c": raw["temperature_2m"],
        "dewpoint_2m_c": raw["dew_point_temperature_2m"],
        "pressure_kpa": raw["pressure_surface"] / 1000.0,
        "wind_speed_10m": np.hypot(raw["wind_u_10m"], raw["wind_v_10m"]),
        "sw_down_wm2": raw["downward_short_wave_radiation_flux_surface"],
        "lw_down_wm2": raw["downward_long_wave_radiation_flux_surface"],
    }


def mrms_convert(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {"precip_mm_h": raw["precipitation_surface"] * 3600.0}


def gefs_convert(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        "precip_mm_h": raw["precipitation_surface"] * 3600.0,
        "temp_2m_c": raw["temperature_2m"],
        "dewpoint_2m_c": dewpoint_from_rh(raw["temperature_2m"], raw["relative_humidity_2m"]),
        "pressure_kpa": raw["pressure_surface"] / 1000.0,
        "wind_speed_10m": np.hypot(raw["wind_u_10m"], raw["wind_v_10m"]),
        "sw_down_wm2": raw["downward_short_wave_radiation_flux_surface"],
        "lw_down_wm2": raw["downward_long_wave_radiation_flux_surface"],
    }


HRRR_VARS = (
    "precipitation_surface", "temperature_2m", "dew_point_temperature_2m", "pressure_surface",
    "wind_u_10m", "wind_v_10m", "downward_short_wave_radiation_flux_surface", "downward_long_wave_radiation_flux_surface",
)
GEFS_VARS = (
    "precipitation_surface", "temperature_2m", "relative_humidity_2m", "pressure_surface",
    "wind_u_10m", "wind_v_10m", "downward_short_wave_radiation_flux_surface", "downward_long_wave_radiation_flux_surface",
)
AORC_VARS = (
    "APCP_surface", "TMP_2maboveground", "SPFH_2maboveground", "PRES_surface",
    "UGRD_10maboveground", "VGRD_10maboveground", "DSWRF_surface", "DLWRF_surface",
)


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "analysis" (time, y, x) or "forecast" (init, [member], lead, y, x)
    raw_vars: tuple[str, ...]
    outputs: tuple[str, ...]
    supersample: int
    bucket: str = ""
    prefix: str = ""
    region: str = "us-west-2"
    members: int = 0  # forecast members kept (0 = deterministic)
    leads: int = 0
    block: int = 1  # leading-dim length of one work block
    # Strict: no valid-weight accumulator; any missing cell makes that value NaN. Used for forecasts, whose
    # gaps are whole steps (e.g. lead-0 precipitation) and whose accumulators are the largest.
    strict: bool = False
    extra: dict = field(default_factory=dict)

    def convert(self, raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        return CONVERTERS[self.name](raw)


CONVERTERS = {
    "aorc": aorc_convert,
    "hrrr_analysis": hrrr_convert,
    "hrrr_forecast": hrrr_convert,
    "mrms": mrms_convert,
    "gefs_forecast": gefs_convert,
    "gefs_forecast_bands": gefs_convert,
}
HRRR_OUT = OUTPUTS[:7]
SOURCES = {
    "aorc": Source("aorc", "analysis", AORC_VARS, OUTPUTS, 10, "noaa-nws-aorc-v1-1-1km", "", "", block=288),
    "hrrr_analysis": Source("hrrr_analysis", "analysis", HRRR_VARS, HRRR_OUT, 10, "dynamical-noaa-hrrr", "noaa-hrrr-analysis/v0.2.0.icechunk", block=2160),
    "mrms": Source("mrms", "analysis", ("precipitation_surface",), ("precip_mm_h",), 5, "dynamical-noaa-mrms", "noaa-mrms-conus-analysis-hourly/v0.3.0.icechunk", block=648),
    "hrrr_forecast": Source("hrrr_forecast", "forecast", HRRR_VARS, HRRR_OUT, 10, "dynamical-noaa-hrrr", "noaa-hrrr-forecast-48-hour/v0.1.0.icechunk", leads=49, block=4, strict=True),
    # 64 steps = 0-189 h at 3 h: covers a 168 h horizon from issue times up to ~21 h after the 00Z run.
    "gefs_forecast": Source("gefs_forecast", "forecast", GEFS_VARS, HRRR_OUT, 20, "dynamical-noaa-gefs", "noaa-gefs-forecast-35-day/v0.2.0.icechunk", members=11, leads=64, block=1, strict=True),
}
# v1.1: operational GEFS again, for elevation-band units only (added alongside the GEFSv12 reforecast).
SOURCES["gefs_forecast_bands"] = replace(SOURCES["gefs_forecast"], name="gefs_forecast_bands")


@cache
def icechunk_group(bucket: str, prefix: str, region: str) -> zarr.Group:
    storage = icechunk.s3_storage(bucket=bucket, prefix=prefix, region=region, anonymous=True)
    repo = icechunk.Repository.open(storage)
    return zarr.open_group(repo.readonly_session("main").store, mode="r")


@cache
def aorc_group(year: int) -> zarr.Group:
    return zarr.open_group(f"s3://noaa-nws-aorc-v1-1-1km/{year}.zarr", mode="r", storage_options={"anon": True})


def group_for(src: Source, year: int | None = None) -> zarr.Group:
    return aorc_group(year) if src.name == "aorc" else icechunk_group(src.bucket, src.prefix, src.region)


def read_raw(arr: zarr.Array, index: tuple) -> np.ndarray:
    """Read a slab as float32, applying CF scale/offset and fill values (AORC is packed int16)."""
    data = arr[index]
    attrs = arr.attrs
    if np.issubdtype(data.dtype, np.integer):
        fill = attrs.get("_FillValue", attrs.get("missing_value"))
        out = data.astype(np.float32)
        if fill is not None:
            out[data == int(fill)] = np.nan
        out *= np.float32(attrs.get("scale_factor", 1.0))
        out += np.float32(attrs.get("add_offset", 0.0))
        return out
    return data.astype(np.float32, copy=False)


def hrrr_grid(src: Source) -> grids.Grid:
    g = group_for(src)
    x, y = g["x"][:], g["y"][:]
    chunks = g["temperature_2m"].chunks
    return grids.hrrr(src.name, g["spatial_ref"].attrs["crs_wkt"], x, y, chunks[-2], chunks[-1])


def grid_for(src: Source) -> grids.Grid:
    match src.name:
        case "aorc":
            return grids.AORC
        case "mrms":
            return grids.MRMS
        case "gefs_forecast" | "gefs_forecast_bands":
            return grids.GEFS
        case "hrrr_analysis" | "hrrr_forecast":
            return hrrr_grid(src)
        case _:
            raise ValueError(src.name)
