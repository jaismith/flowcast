"""Live model inputs for one basin and issue, in the training cube's feature vocabulary.

Definitions follow the cube builders exactly: `qobs_mm_h = q * 3.6 / area` from hour-ending mean discharge (m3/s);
`gauged_outflow_mm_h`, the sum of the basin's below-dam gauges (NaN if any is missing); `q_flat_run_h` and
`q_constant_release` from `flowcast_pipeline.dataset.cube`; `upstream_tw_c` / `outflow_tw_c` as `tempcube` chose
their gauges (recorded per basin in the temperature version's `temp_gauges.json`); calendar harmonics from
`tempcube.time_harmonics`. Nothing after the issue time is used: observed inputs are cut at the issue hour, and
SNODAS products count only once published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from flowcast_model.tempcube import time_harmonics
from flowcast_pipeline.dataset.cube import CONSTANT_RELEASE_MIN_H, flat_run_hours
from flowcast_pipeline.lake import Lake

from . import snodas_live, usgs_inputs
from .forcing import ANALYSIS, load_analysis
from .livecube import LiveBasin, Product

HRRR_VARS = ["precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2"]
GEFS_VARS = HRRR_VARS
# Window kept before the issue: the flow model's 720 h hindcast + 1 h lag, plus a day of slack for date rounding;
# flat-run counts (capped at 720 h) need the history before that.
HISTORY_DAYS = 33
FLAT_RUN_HISTORY_DAYS = 45


@dataclass
class BasinMeta:
    basin: str
    area_km2: float
    below_dam: bool
    lon: float
    outflow_gauges: list[str]
    upstream_tw_gauge: str | None
    outflow_tw_gauges: list[str]


def window(issue: pd.Timestamp, forecast_h: int = 180) -> pd.DatetimeIndex:
    start = (issue - pd.Timedelta(days=HISTORY_DAYS)).floor("D")
    end = (issue + pd.Timedelta(hours=forecast_h)).floor("D") + pd.Timedelta(hours=23)
    return pd.date_range(start, end, freq="h", tz="UTC")


def gauges_for(meta: BasinMeta, with_temperature: bool) -> dict[str, list[str]]:
    q = [meta.basin, *meta.outflow_gauges]
    tw = [meta.basin, *([meta.upstream_tw_gauge] if meta.upstream_tw_gauge else []), *meta.outflow_tw_gauges] if with_temperature else []
    return {"discharge": q, "water_temperature": tw}


def _cut(s: pd.Series, issue: pd.Timestamp) -> pd.Series:
    return s.where(s.index <= issue)


def observed(lake: Lake, meta: BasinMeta, index: pd.DatetimeIndex, issue: pd.Timestamp, with_temperature: bool) -> pd.DataFrame:
    long_start = issue - pd.Timedelta(days=FLAT_RUN_HISTORY_DAYS)
    full = pd.date_range(min(long_start.floor("h"), index[0]), index[-1], freq="h", tz="UTC")
    q = _cut(usgs_inputs.load(lake, meta.basin, "discharge", full[0], full[-1]), issue)
    out = pd.DataFrame(index=full)
    out["qobs_m3s"] = q.astype(np.float32)
    out["qobs_mm_h"] = (q * 3.6 / meta.area_km2).astype(np.float32)
    run = flat_run_hours(q.to_numpy(np.float32))
    with np.errstate(invalid="ignore"):
        steady = (run >= CONSTANT_RELEASE_MIN_H) & (q.to_numpy() > 0) & meta.below_dam
    out["q_flat_run_h"] = run
    out["q_constant_release"] = np.where(np.isfinite(q.to_numpy()), steady.astype(np.float32), np.nan).astype(np.float32)
    if meta.outflow_gauges:
        total = sum(_cut(usgs_inputs.load(lake, g, "discharge", full[0], full[-1]), issue) for g in meta.outflow_gauges)
        out["gauged_outflow_mm_h"] = (total * 3.6 / meta.area_km2).astype(np.float32)
    else:
        out["gauged_outflow_mm_h"] = np.float32(np.nan)
    if with_temperature:
        out["tw_c"] = _cut(usgs_inputs.load(lake, meta.basin, "water_temperature", full[0], full[-1]), issue).astype(np.float32)
        up = meta.upstream_tw_gauge
        out["upstream_tw_c"] = _cut(usgs_inputs.load(lake, up, "water_temperature", full[0], full[-1]), issue).astype(np.float32) if up else np.float32(np.nan)
        if meta.outflow_tw_gauges:
            stack = np.stack([_cut(usgs_inputs.load(lake, g, "water_temperature", full[0], full[-1]), issue).to_numpy(np.float32) for g in meta.outflow_tw_gauges])
            with np.errstate(all="ignore"):
                out["outflow_tw_c"] = np.nanmean(stack, axis=0).astype(np.float32)
        else:
            out["outflow_tw_c"] = np.float32(np.nan)
    return out.reindex(index)


def forcing(lake: Lake, basin: str, index: pd.DatetimeIndex, issue: pd.Timestamp) -> pd.DataFrame:
    out = pd.DataFrame(index=index)
    for prefix in ANALYSIS:
        cols = HRRR_VARS if prefix == "hrrr_an" else ["precip_mm_h"]
        df = load_analysis(lake, prefix, basin, index[0], index[-1], cols)
        for c in cols:
            out[f"{prefix}_{c}"] = df[c].where(df.index <= issue)
    snow = snodas_live.hourly(lake, basin, index, issue)
    for c in snow.columns:
        out[c] = snow[c].where(snow.index <= issue)
    return out


def gefs_product(init: pd.Timestamp, leads: np.ndarray, basin_values: np.ndarray) -> Product:
    """`basin_values[member, lead, var]` (basin unit) as the cube's `gefs_*` product."""
    return Product(pd.DatetimeIndex([init]), leads, {f"gefs_{v}": basin_values[None, :, :, j] for j, v in enumerate(GEFS_VARS)})


def flowfc_product(issue: pd.Timestamp, flow_member_median_mm_h: np.ndarray) -> Product:
    """The flow forecast as the temperature model's `flowfc_qobs_mm_h` product: [member, lead 1..168] (mm/h)."""
    leads = np.arange(1, flow_member_median_mm_h.shape[1] + 1, dtype=float)
    return Product(pd.DatetimeIndex([issue]), leads, {"flowfc_qobs_mm_h": flow_member_median_mm_h[None]})


def statics(version_root: Path, basin: str, names: list[str]) -> dict[str, float]:
    table = pd.read_parquet(version_root / "statics.parquet")
    if basin not in table.index:
        raise KeyError(f"basin {basin} has no statics in {version_root}")
    row = table.loc[basin]
    return {n: float(row[n]) for n in names if n in row.index} | {"area_km2": float(row["area_km2"])}


def basin_meta(flow_root: Path, temp_root: Path | None, basin: str) -> BasinMeta:
    st = pd.read_parquet(flow_root / "statics.parquet").loc[basin]
    outflows = json.loads((flow_root / "outflows.json").read_text()).get(basin, [])
    temp = json.loads((temp_root / "temp_gauges.json").read_text()).get(basin, {}) if temp_root else {}
    return BasinMeta(basin, float(st["area_km2"]), bool(st.get("below_dam", 0) > 0), float(st["lon"]), list(outflows), temp.get("upstream"), list(temp.get("outflow", [])))


def build(lake: Lake, meta: BasinMeta, issue: pd.Timestamp, static_values: dict[str, float], with_temperature: bool,
          products: dict[str, Product], obs: pd.DataFrame | None = None, frc: pd.DataFrame | None = None) -> LiveBasin:
    index = window(issue)
    obs = observed(lake, meta, index, issue, with_temperature) if obs is None else obs
    frc = forcing(lake, meta.basin, index, issue) if frc is None else frc
    dyn = pd.concat([obs, frc], axis=1)
    if with_temperature:
        for name, arr in time_harmonics(index.tz_convert(None), np.array([meta.lon])).items():
            dyn[name] = arr[0]
    return LiveBasin(meta.basin, dyn, static_values, products)
