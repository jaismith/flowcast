"""Target unit conversions to the harness's discharge unit (ft3/s)."""

import numpy as np

M3_S_TO_CFS = 35.314666721
# 1 mm/h of runoff over 1 km2 = 1e3 m3 per 3600 s
MM_H_KM2_TO_M3_S = 1.0 / 3.6


def to_cfs(values: np.ndarray, unit: str, area_km2: float | None) -> np.ndarray:
    unit = unit.lower()
    if unit in ("ft3/s", "cfs"):
        return values
    if unit in ("m3/s", "m3 s-1"):
        return values * M3_S_TO_CFS
    if unit in ("mm/h", "mm h-1", "mm/hr"):
        if not area_km2:
            raise ValueError("converting mm/h to ft3/s needs the drainage area")
        return values * MM_H_KM2_TO_M3_S * area_km2 * M3_S_TO_CFS
    raise ValueError(f"unsupported target unit {unit!r}")
