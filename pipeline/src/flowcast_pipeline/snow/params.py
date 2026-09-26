"""SNOW-17 parameters and the flowcast extensions (radiation melt, wet-bulb split, humidity/wind rain-on-snow).

Classic SNOW-17 parameters follow Anderson (2006), "Snow Accumulation and Ablation Model - SNOW-17",
in the same units: melt factors in mm/degC per 6 h, UADJ in mm/mb per 6 h, DAYGM in mm/day.
"""

from dataclasses import asdict, dataclass, field, replace
from enum import IntEnum

import numpy as np


class MeltMode(IntEnum):
    CLASSIC = 0  # seasonal melt factor x (Ta - MBASE)
    RADIATION = 1  # temperature factor x (Ta - MBASE) + absorbed terrain-corrected shortwave


class PrecipSplit(IntEnum):
    AIR_TEMPERATURE = 0  # all snow at Ta <= PXTEMP
    WET_BULB = 1  # linear ramp in wet-bulb temperature between tw_snow and tw_rain


class RainOnSnow(IntEnum):
    CLASSIC = 0  # 90% relative humidity, constant UADJ
    HUMIDITY_WIND = 1  # forcing vapor pressure, wind function proportional to wind speed


ADC_NORTHEAST = (0.05, 0.15, 0.26, 0.37, 0.47, 0.57, 0.67, 0.77, 0.87, 0.95, 1.0)


@dataclass(frozen=True)
class SnowParams:
    # Classic SNOW-17.
    scf: float = 1.0
    mfmax: float = 1.0
    mfmin: float = 0.2
    uadj: float = 0.05
    si: float = 100.0
    nmf: float = 0.15
    tipm: float = 0.1
    mbase: float = 0.0
    pxtemp: float = 1.0
    plwhc: float = 0.05
    daygm: float = 0.3
    adc: tuple[float, ...] = ADC_NORTHEAST
    # New-snow threshold for leaving the depletion curve, mm/h (HSNOF in EXSNOW19).
    snof: float = 0.2

    melt_mode: MeltMode = MeltMode.RADIATION
    precip_split: PrecipSplit = PrecipSplit.WET_BULB
    rain_on_snow: RainOnSnow = RainOnSnow.HUMIDITY_WIND

    # Wet-bulb split: all snow at Tw <= tw_snow, all rain at Tw >= tw_rain (degC).
    tw_snow: float = -0.5
    tw_rain: float = 1.5

    # Radiation melt: M = tf*(Ta - MBASE)+ + srf * (1 - albedo) * SW * 3600 / Lf, applied when Ta > rad_tmin.
    tf: float = 0.04  # mm/degC/h
    srf: float = 1.0  # fraction of absorbed shortwave that goes to melt
    rad_tmin: float = 0.0  # degC
    albedo_fresh: float = 0.85
    albedo_old: float = 0.50
    albedo_tau_h: float = 240.0  # e-folding snow age (h); ages 3x faster when Ta > 0
    albedo_refresh_mm: float = 5.0  # new snow (mm SWE) that resets age by a factor e

    # Humidity/wind rain-on-snow: UADJ = max(wind_function * u_eff, uadj_min), mm/mb/6h, u_eff in m/s.
    wind_function: float = 0.02
    uadj_min: float = 0.01

    # Canopy: shortwave transmissivity and wind reduction for the forested fraction of an HRU.
    canopy_sw_transmissivity: float = 0.45
    canopy_wind_factor: float = 0.5

    # Lapse rates and orographic precipitation for basin-mean forcing broadcast to bands.
    temp_lapse_c_per_km: float = -6.0
    precip_gradient_per_km: float = 0.0

    def replace(self, **changes) -> "SnowParams":
        return replace(self, **changes)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("melt_mode", "precip_split", "rain_on_snow"):
            d[key] = int(d[key])
        d["adc"] = list(self.adc)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SnowParams":
        d = dict(d)
        d["melt_mode"] = MeltMode(d.get("melt_mode", MeltMode.RADIATION))
        d["precip_split"] = PrecipSplit(d.get("precip_split", PrecipSplit.WET_BULB))
        d["rain_on_snow"] = RainOnSnow(d.get("rain_on_snow", RainOnSnow.HUMIDITY_WIND))
        if "adc" in d:
            d["adc"] = tuple(d["adc"])
        return cls(**d)


CLASSIC = SnowParams(
    melt_mode=MeltMode.CLASSIC,
    precip_split=PrecipSplit.AIR_TEMPERATURE,
    rain_on_snow=RainOnSnow.CLASSIC,
)

# Regional defaults used at every site. See docs/snow-radiation-module.md for how they were chosen.
NORTHEAST = SnowParams()

KERNEL_PARAM_NAMES = (
    "scf", "mfmax", "mfmin", "uadj", "si", "nmf", "tipm", "mbase", "plwhc", "daygm", "snof",
    "tf", "srf", "rad_tmin", "albedo_fresh", "albedo_old", "albedo_tau_h", "albedo_refresh_mm",
    "wind_function", "uadj_min", "latitude", "forest_frac", "canopy_wind_factor", "canopy_sw_transmissivity",
)  # fmt: skip


@dataclass
class HruParams:
    """Per-HRU parameter matrix for the kernel: columns follow KERNEL_PARAM_NAMES."""

    values: np.ndarray
    adc: np.ndarray
    flags: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.int64))

    @classmethod
    def build(cls, params: SnowParams, latitude: np.ndarray, forest_frac: np.ndarray) -> "HruParams":
        n = len(latitude)
        cols = []
        for name in KERNEL_PARAM_NAMES:
            if name == "latitude":
                cols.append(np.asarray(latitude, dtype=np.float64))
            elif name == "forest_frac":
                cols.append(np.asarray(forest_frac, dtype=np.float64))
            else:
                cols.append(np.full(n, float(getattr(params, name))))
        flags = np.array([int(params.melt_mode), int(params.precip_split), int(params.rain_on_snow)], dtype=np.int64)
        return cls(np.column_stack(cols), np.asarray(params.adc, dtype=np.float64), flags)
