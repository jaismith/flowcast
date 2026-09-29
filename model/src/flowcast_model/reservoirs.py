"""NYC Delaware reservoir storage as a model input (dam-release experiment, rebuild plan §4.1).

Builds a small store of hourly `(basin, time)` inputs for the basins below Cannonsville, Pepacton or Neversink, read
next to a training or temperature cube (`flowcast.dataset.cube: [cube, reservoirs.zarr]`); other basins have none.

Daily usable storage (million gallons) per reservoir, in order of preference:

* NYC DEP daily storage 2000-2021, as compiled by Pywr-DRB (`NYC_storage_daily_2000-2021.csv`, MIT);
* NYC Open Data "Current Reservoir Levels" (zkky-n5j3, Nov 2017 - Sep 2025). Its storage columns are mislabelled:
  Cannonsville's storage is under `pepacton_conservation_flow_release`, Pepacton's under `cannonsville_release`;
* USGS reservoir-elevation gauges converted to storage (Pywr-DRB `usgs_nyc_storage_mg.csv`, from late 2019), which
  count total rather than usable storage: shifted by the median offset to the DEP series where they overlap.

Inputs (all derived from storage and the date, so available in real time; USGS elevation is reported every 15 min):

| input | meaning |
|---|---|
| `res_up_frac` | usable storage / capacity of the NYC reservoirs upstream of the basin |
| `res_nyc_frac` | combined storage fraction of all three, which sets the FFMP release zone |
| `res_up_space_mm` | capacity minus storage upstream, as mm over the basin (negative while spilling) |
| `res_ffmp_margin_l1c`, `res_ffmp_margin_l2` | combined fraction minus the FFMP L1-c and L2 zone curves on that date |
| `res_ffmp_factor` | FFMP conservation-release factor of the zone on that date, mean over the upstream reservoirs |
| `res_doy_sin`, `res_doy_cos` | day-of-year harmonics, only where storage is available |

Timing: the value at hour-ending t is the previous local day's storage (18-48 h old), a conservative stand-in for
the real-time feeds. Nothing at or after the frozen test start (2022-10-01) is written.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xarray as xr

from .cube import FROZEN_TEST_START

log = logging.getLogger(__name__)

PYWR = "https://raw.githubusercontent.com/Pywr-DRB/Pywr-DRB/master/src/pywrdrb/data"
SOURCES = {
    "NYC_storage_daily_2000-2021.csv": f"{PYWR}/observations/_raw/NYC_storage_daily_2000-2021.csv",
    "usgs_nyc_storage_mg.csv": f"{PYWR}/observations/_raw/usgs_nyc_storage_mg.csv",
    "ffmp_reservoir_operation_daily_profiles.csv": f"{PYWR}/operational_constants/ffmp_reservoir_operation_daily_profiles.csv",
    "nyc_opendata_reservoirs.csv": "https://data.cityofnewyork.us/resource/zkky-n5j3.csv?$limit=50000",
}
RESERVOIRS = ("cannonsville", "pepacton", "neversink")
# usable capacity, million gallons (NYC DEP; the DEP series' PercentageTotal divides by their sum)
CAPACITY_MG = {"cannonsville": 95_706.0, "pepacton": 140_190.0, "neversink": 34_941.0}
# the USGS gauge just below each dam: a basin is below a reservoir if it is that gauge or lists it as gauged outflow
RELEASE_GAUGE = {"cannonsville": "01425000", "pepacton": "01417000", "neversink": "01436000"}
OPENDATA_COLUMNS = {"cannonsville": "pepacton_conservation_flow_release", "pepacton": "cannonsville_release", "neversink": "neversink_storage"}
FFMP_CURVES = ("level1b", "level1c", "level2", "level3", "level4", "level5")
FFMP_ZONES = ("level1a", "level1b", "level1c", "level2", "level3", "level4", "level5")
MG_TO_M3 = 3785.411784
TIMEZONE = "America/New_York"
FEATURES = ("res_up_frac", "res_nyc_frac", "res_up_space_mm", "res_ffmp_margin_l1c", "res_ffmp_margin_l2", "res_ffmp_factor", "res_doy_sin", "res_doy_cos")


def download(src_dir: str | Path) -> Path:
    src_dir = Path(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        path = src_dir / name
        if not path.exists():
            log.info("download %s", url)
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            path.write_bytes(resp.content)
    return src_dir


def _daily(df: pd.DataFrame) -> pd.DataFrame:
    df.index = pd.DatetimeIndex(df.index).normalize()
    return df[~df.index.duplicated()].sort_index()


def daily_storage(src_dir: str | Path) -> tuple[pd.DataFrame, dict]:
    """Daily usable storage (MG) per reservoir, and provenance: rows per source and the USGS offsets applied."""
    src_dir = Path(src_dir)
    dep = _daily(pd.read_csv(src_dir / "NYC_storage_daily_2000-2021.csv", index_col=0, parse_dates=True)[list(RESERVOIRS)])
    od = pd.read_csv(src_dir / "nyc_opendata_reservoirs.csv", parse_dates=["neversink_date"]).set_index("neversink_date")
    od = _daily(od[[OPENDATA_COLUMNS[r] for r in RESERVOIRS]].set_axis(list(RESERVOIRS), axis=1) * 1000.0)
    usgs = _daily(pd.read_csv(src_dir / "usgs_nyc_storage_mg.csv", index_col=0, parse_dates=True)[list(RESERVOIRS)])
    # Open Data has isolated zero and near-zero readings; storage never falls below 25% of capacity in the record
    od = od.where(od.gt(pd.Series({r: 0.25 * CAPACITY_MG[r] for r in RESERVOIRS}), axis=1))
    offsets = {r: float((usgs[r] - dep[r]).dropna().median()) for r in RESERVOIRS}
    index = pd.date_range(dep.index.min(), max(od.index.max(), usgs.index.max()), freq="D")
    out = dep.reindex(index)
    source = pd.DataFrame("dep", index=index, columns=list(RESERVOIRS)).where(out.notna())
    for name, frame in (("opendata", od), ("usgs", usgs - pd.Series(offsets))):
        fill = out.isna() & frame.reindex(index).notna()
        out = out.where(~fill, frame.reindex(index))
        source = source.where(~fill, name)
    counts = {r: source[r].value_counts().to_dict() for r in RESERVOIRS}
    return out, {"usgs_offset_mg": offsets, "days_by_source": counts}


def ffmp_profiles(src_dir: str | Path) -> pd.DataFrame:
    """FFMP zone curves and release factors by day of year (1-366), from Pywr-DRB's daily profiles."""
    df = pd.read_csv(Path(src_dir) / "ffmp_reservoir_operation_daily_profiles.csv", index_col=0).T
    df.index = df["doy"].astype(int)
    return df.drop(columns="doy").astype(float)


def upstream_reservoirs(basin: str, outflow_sites: str) -> list[str]:
    sites = {s for s in str(outflow_sites).replace(",", " ").split() if s.isdigit()} | {basin}
    return [r for r in RESERVOIRS if RELEASE_GAUGE[r] in sites]


def daily_features(storage: pd.DataFrame, profiles: pd.DataFrame, upstream: list[str], area_km2: float) -> pd.DataFrame:
    """Per-day inputs for one basin below `upstream` reservoirs (NaN where any needed storage is missing)."""
    total = sum(CAPACITY_MG.values())
    nyc = storage[list(RESERVOIRS)].sum(axis=1, min_count=len(RESERVOIRS)) / total
    up = storage[upstream].sum(axis=1, min_count=len(upstream))
    cap = sum(CAPACITY_MG[r] for r in upstream)
    doy = storage.index.dayofyear.to_numpy()
    curves = profiles.reindex(doy)
    below = np.stack([nyc.to_numpy() < curves[c].to_numpy() for c in FFMP_CURVES], axis=1).sum(axis=1)
    zone = np.array(FFMP_ZONES)[below]
    factor = np.mean([[curves[f"{z}_factor_mrf_{r}"].to_numpy()[i] for i, z in enumerate(zone)] for r in upstream], axis=0)
    phase = 2 * np.pi * (doy - 1) / 365.25
    df = pd.DataFrame(
        {
            "res_up_frac": up / cap,
            "res_nyc_frac": nyc,
            "res_up_space_mm": (cap - up) * MG_TO_M3 / (area_km2 * 1e6) * 1000.0,
            "res_ffmp_margin_l1c": nyc - curves["level1c"].to_numpy(),
            "res_ffmp_margin_l2": nyc - curves["level2"].to_numpy(),
            "res_ffmp_factor": factor,
            "res_doy_sin": np.sin(phase),
            "res_doy_cos": np.cos(phase),
        },
        index=storage.index,
    )
    return df.where(nyc.notna() & up.notna(), axis=0)


def hourly(daily: pd.DataFrame, times: pd.DatetimeIndex) -> np.ndarray:
    """(feature, time) at hour-ending UTC `times`: the value of the local day before the hour's local date."""
    local = (times - pd.Timedelta(hours=1)).tz_localize("UTC").tz_convert(TIMEZONE).tz_localize(None).normalize()
    day = local - pd.Timedelta(days=1)
    return daily.reindex(day).to_numpy(np.float32).T


def build(cube: str, out: str | Path, src_dir: str | Path, sites_cube: str | None = None) -> dict:
    """Write the reservoir store for the basins of `cube` (a trainval store) that sit below an NYC reservoir.

    `sites_cube`: where to read `gauged_outflow_sites` if `cube` lacks it (the temperature cube keeps numeric
    attributes only), e.g. the training cube it was built from."""
    if cube.rstrip("/").endswith("test.zarr"):
        raise ValueError("built from trainval only; the frozen test store is never read")
    download(src_dir)
    storage, provenance = daily_storage(src_dir)
    profiles = ffmp_profiles(src_dir)
    src = xr.open_zarr(cube, chunks=None, consolidated=None)
    times = pd.DatetimeIndex(src["time"].values)
    times = times[times < FROZEN_TEST_START]
    basins = [str(b) for b in src["basin"].values]
    if "gauged_outflow_sites" in src:
        sites = src["gauged_outflow_sites"].values
    else:
        other = xr.open_zarr(sites_cube, chunks=None, consolidated=None)
        lookup = dict(zip([str(b) for b in other["basin"].values], other["gauged_outflow_sites"].values))
        sites = np.array([lookup.get(b, "") for b in basins])
    areas = src["area_km2"].values.astype(float)
    rows, arrays, upstream = [], [], {}
    for i, b in enumerate(basins):
        up = upstream_reservoirs(b, sites[i])
        if not up:
            continue
        rows.append(b)
        upstream[b] = up
        arrays.append(hourly(daily_features(storage, profiles, up, areas[i]), times))
    if not rows:
        raise ValueError(f"no basin of {cube} is below an NYC reservoir")
    data = np.stack(arrays, axis=1)
    ds = xr.Dataset(
        {f: (("basin", "time"), data[k]) for k, f in enumerate(FEATURES)},
        coords={"basin": np.array(rows, dtype=str), "time": times.values},
        attrs={
            "flowcast_subset": "NYC Delaware reservoir storage inputs (reservoirs.py)",
            "source_cube": cube,
            "sources": ", ".join(SOURCES.values()),
            "upstream": str(upstream),
            "usgs_offset_mg": str(provenance["usgs_offset_mg"]),
        },
    )
    ds.to_zarr(str(out), mode="w", encoding={f: {"chunks": (1, len(times))} for f in FEATURES}, consolidated=True, zarr_format=3)
    valid = {b: float(np.isfinite(data[0, j]).mean()) for j, b in enumerate(rows)}
    log.info("reservoir store %s: %d basins %s, valid fraction %s", out, len(rows), upstream, valid)
    return {"basins": upstream, "valid_fraction": valid, **provenance}
