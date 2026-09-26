"""Snow-module validation against SNODAS basin SWE and GHCN-Daily station SWE (Catskills and other Northeast sites).

    uv run --group snow-validation flowcast-snow-validate fetch        # HRUs, AORC per band, SNODAS, GHCN (cached)
    uv run --group snow-validation flowcast-snow-validate calibrate    # regional parameters on calibration basins/years
    uv run --group snow-validation flowcast-snow-validate evaluate     # metrics tables and plots

There are no SNOTEL sites in the Catskills or the rest of the Northeast study area, so SNODAS (1 km daily, NOHRSC
assimilation) is the basin-scale reference and CoCoRaHS/co-op snow-water-equivalent reports are the point check.
Data are cached under $FLOWCAST_SNOW_VAL_DIR (default /tmp/snowval).
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import requests
import s3fs
import shapely
import shapely.geometry
import xarray as xr
from scipy import optimize

from . import meteo, terrain
from .dem import fetch_dem
from .features import basin_features
from .hru import HRUSet, build_hrus, build_point_hrus, fetch_nldi_basin
from .model import AORC_NAMES, AORC_RADIATION_LABEL, prepare_forcing, run_snow
from .params import CLASSIC, NORTHEAST, HruParams, SnowParams
from .snow17 import N_OUT, OUTPUTS, initial_state, snow17_kernel

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (backend must be set before pyplot is imported)

CACHE = Path(os.environ.get("FLOWCAST_SNOW_VAL_DIR", "/tmp/snowval"))
RESULTS = Path(__file__).resolve().parents[3] / "results" / "snow_validation"
AORC_BUCKET = "noaa-nws-aorc-v1-1-1km"
SNODAS_STORE = "hytest/snodas.zarr"
OSN_ENDPOINT = "https://usgs.osn.mghpcc.org"
GHCN_URL = "https://noaa-ghcn-pds.s3.amazonaws.com/csv/by_station/{id}.csv"

FIRST_WY, LAST_WY = 2007, 2025
CAL_WYS = range(2007, 2016)
VAL_WYS = range(2016, 2026)
SWE_HOUR_UTC = 6  # SNODAS SWE is valid at 06 UTC


@dataclass(frozen=True)
class Basin:
    name: str
    region: str
    cluster: str
    holdout: bool = False


BASINS = {
    "USGS-01423000": Basin("West Branch Delaware at Walton, NY (Cannonsville inflow)", "Catskills", "catskills"),
    "USGS-01413500": Basin("East Branch Delaware at Margaretville, NY (Pepacton inflow)", "Catskills", "catskills"),
    "USGS-01420500": Basin("Beaver Kill at Cooks Falls, NY", "Catskills", "catskills", holdout=True),
    "USGS-01435000": Basin("Neversink River near Claryville, NY", "Catskills", "catskills"),
    "USGS-01350000": Basin("Schoharie Creek at Prattsville, NY", "Catskills", "catskills"),
    "USGS-01362500": Basin("Esopus Creek at Coldbrook, NY", "Catskills", "catskills", holdout=True),
    "USGS-01054200": Basin("Wild River at Gilead, ME", "Northeast", "whites"),
    "USGS-01137500": Basin("Ammonoosuc River at Bethlehem Junction, NH", "Northeast", "whites", holdout=True),
    "USGS-01142500": Basin("Ayers Brook at Randolph, VT", "Northeast", "vermont"),
    "USGS-04256000": Basin("Independence River at Donnattsburg, NY", "Northeast", "adirondacks"),
    "USGS-01333000": Basin("Green River at Williamstown, MA", "Northeast", "berkshires"),
}

# CoCoRaHS / co-op stations reporting snow water equivalent (GHCN-Daily WESD): (lon, lat, elevation m).
STATIONS = {
    "US1NYUL0028": ("Kerhonkson 3.7 N", -74.3012, 41.8321, 273.1),
    "US1NYSV0006": ("Long Eddy", -75.1333, 41.8500, 267.9),
    "US1NYDL0025": ("Hobart 4.8 ESE", -74.5783, 42.3523, 676.7),
    "US1NYDL0023": ("Long Eddy 6.5 NNE", -75.0784, 41.9351, 532.2),
    "US1NYGR0014": ("Lexington 1.5 N", -74.3587, 42.2602, 583.1),
    "USC00302366": ("East Jewett", -74.1444, 42.2344, 606.9),
    "US1NYDL0032": ("Delhi 6.6 WNW", -75.0299, 42.3212, 616.3),
}
STATION_CLUSTER = "catskills"

AORC_VARS = list(AORC_NAMES)[:7]


def water_year_bounds(wy: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp(f"{wy - 1}-10-01 01:00"), pd.Timestamp(f"{wy}-10-01 00:00")


# ----------------------------------------------------------------------------------------------------------------
# Fetch


def basin_hrus(basin_id: str) -> HRUSet:
    path = CACHE / "hrus" / basin_id
    if (path / "hrus.parquet").exists():
        return HRUSet.load(path)
    geom = fetch_nldi_basin(basin_id)
    hrus = build_hrus({basin_id: geom}, n_bands=4, n_aspects=2)
    hrus.save(path)
    return hrus


def station_hrus() -> HRUSet:
    path = CACHE / "hrus" / "stations"
    if (path / "hrus.parquet").exists():
        return HRUSet.load(path)
    hrus = build_point_hrus({sid: (lon, lat, z) for sid, (_, lon, lat, z) in STATIONS.items()})
    hrus.save(path)
    return hrus


def _bbox_indices(coord: np.ndarray, lo: float, hi: float) -> slice:
    idx = np.where((coord >= lo) & (coord <= hi))[0]
    return slice(int(idx[0]), int(idx[-1]) + 1)


def _cluster_targets(cluster: str, lat: np.ndarray, lon: np.ndarray):
    """Weight matrices (band means) and nearest-cell indices (stations) for one cluster on a lat/lon grid."""
    basins = [b for b, meta in BASINS.items() if meta.cluster == cluster]
    weights = {}
    bounds = []
    for b in basins:
        hrus = basin_hrus(b)
        w = hrus.grid_weights(lat, lon)
        weights[b] = w
        bounds.append((w["i_lat"].min(), w["i_lat"].max(), w["j_lon"].min(), w["j_lon"].max()))
    stations = {}
    if cluster == STATION_CLUSTER:
        for sid, (_, slon, slat, _) in STATIONS.items():
            i, j = int(np.abs(lat - slat).argmin()), int(np.abs(lon - slon).argmin())
            stations[sid] = (i, j)
            bounds.append((i, i, j, j))
    b = np.array(bounds)
    ys = slice(int(b[:, 0].min()), int(b[:, 1].max()) + 1)
    xs = slice(int(b[:, 2].min()), int(b[:, 3].max()) + 1)
    ny, nx = ys.stop - ys.start, xs.stop - xs.start
    mats = {}
    for basin, w in weights.items():
        ids = sorted(w["band_id"].unique())
        m = np.zeros((len(ids), ny * nx))
        for k, band in enumerate(ids):
            sub = w[w["band_id"] == band]
            m[k, (sub["i_lat"] - ys.start) * nx + (sub["j_lon"] - xs.start)] += sub["weight"].to_numpy()
        mats[basin] = (ids, m)
    stations = {sid: ((i - ys.start) * nx + (j - xs.start)) for sid, (i, j) in stations.items()}
    return ys, xs, mats, stations


def fetch_aorc(years: list[int], clusters: list[str] | None = None) -> None:
    fs = s3fs.S3FileSystem(anon=True)
    clusters = clusters or sorted({b.cluster for b in BASINS.values()})
    for year in years:
        ds = xr.open_zarr(s3fs.S3Map(f"{AORC_BUCKET}/{year}.zarr", s3=fs), consolidated=True)
        lat, lon = ds["latitude"].to_numpy(), ds["longitude"].to_numpy()
        for cluster in clusters:
            done = CACHE / "aorc" / f"{cluster}_{year}.done"
            if done.exists():
                continue
            ys, xs, mats, stations = _cluster_targets(cluster, lat, lon)
            months = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="MS")
            times = pd.DatetimeIndex(ds["time"].to_numpy())

            def month_block(k):
                sel = np.where((times >= months[k]) & (times < months[k + 1]))[0]
                block = ds[AORC_VARS].isel(time=slice(int(sel[0]), int(sel[-1]) + 1), latitude=ys, longitude=xs).load()
                nt = block.sizes["time"]
                flat = {v: block[v].to_numpy().reshape(nt, -1).astype(np.float64) for v in AORC_VARS}
                out = {}
                for basin, (ids, m) in mats.items():
                    out[basin] = {f"{v}|{band}": flat[v] @ m[k2] for v in AORC_VARS for k2, band in enumerate(ids)}
                for sid, cell in stations.items():
                    out[sid] = {f"{v}|{sid}": flat[v][:, cell] for v in AORC_VARS}
                return pd.DatetimeIndex(block["time"].to_numpy()), out

            with ThreadPoolExecutor(3) as pool:
                parts = list(pool.map(month_block, range(12)))
            index = pd.DatetimeIndex(np.concatenate([p[0] for p in parts]))
            for target in parts[0][1]:
                frame = pd.DataFrame({c: np.concatenate([p[1][target][c] for p in parts]) for c in parts[0][1][target]}, index=index)
                path = CACHE / "aorc" / target / f"{year}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                frame.astype(np.float32).to_parquet(path)
            done.write_text("ok")
            print(f"aorc {cluster} {year}: {len(index)} h", flush=True)


def fetch_snodas() -> None:
    osn = s3fs.S3FileSystem(anon=True, client_kwargs={"endpoint_url": OSN_ENDPOINT})
    ds = xr.open_zarr(s3fs.S3Map(SNODAS_STORE, s3=osn))
    lat, lon = ds["lat"].to_numpy(), ds["lon"].to_numpy()
    for cluster in sorted({b.cluster for b in BASINS.values()}):
        if all((CACHE / "snodas" / f"{b}.parquet").exists() for b, m in BASINS.items() if m.cluster == cluster):
            continue
        ys, xs, mats, stations = _cluster_targets(cluster, lat, lon)
        block = ds[["SWE", "SNM"]].isel(lat=ys, lon=xs).sel(time=slice(f"{FIRST_WY - 1}-10-01", f"{LAST_WY}-09-30")).load()
        nt = block.sizes["time"]
        swe = block["SWE"].to_numpy().reshape(nt, -1).astype(np.float64) * 1000.0
        snm = block["SNM"].to_numpy().reshape(nt, -1).astype(np.float64) * 1000.0
        index = pd.DatetimeIndex(block["time"].to_numpy())
        for basin, (ids, m) in mats.items():
            valid = np.isfinite(swe) & (swe >= 0)
            cols = {}
            for k, band in enumerate(ids):
                w = m[k]
                wv = valid * w[None, :]
                denom = wv.sum(axis=1)
                cols[f"swe|{band}"] = np.where(denom > 0.9, np.nansum(np.where(valid, swe, 0) * w, axis=1) / denom, np.nan)
                cols[f"snm|{band}"] = np.where(denom > 0.9, np.nansum(np.where(valid, snm, 0) * w, axis=1) / denom, np.nan)
                cols[f"sca|{band}"] = np.where(denom > 0.9, np.nansum(np.where(valid, swe > 5.0, 0) * w, axis=1) / denom, np.nan)
            path = CACHE / "snodas" / f"{basin}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(cols, index=index).to_parquet(path)
        for sid, cell in stations.items():
            path = CACHE / "snodas" / f"{sid}.parquet"
            pd.DataFrame({"swe": swe[:, cell]}, index=index).to_parquet(path)
        print(f"snodas {cluster}: {nt} days", flush=True)


def fetch_ghcn() -> None:
    for sid in STATIONS:
        path = CACHE / "ghcn" / f"{sid}.parquet"
        if path.exists():
            continue
        resp = requests.get(GHCN_URL.format(id=sid), timeout=120)
        resp.raise_for_status()
        raw = pd.read_csv(pd.io.common.StringIO(resp.text), dtype={"M_FLAG": str, "Q_FLAG": str, "S_FLAG": str, "OBS_TIME": str})
        raw = raw[raw["ELEMENT"].isin(["WESD", "SNWD"]) & raw["Q_FLAG"].isna()]
        wide = raw.pivot_table(index="DATE", columns="ELEMENT", values="DATA_VALUE", aggfunc="first")
        wide.index = pd.to_datetime(wide.index.astype(str))
        out = pd.DataFrame(index=wide.index)
        out["wesd_mm"] = wide.get("WESD", np.nan) / 10.0
        out["snwd_mm"] = wide.get("SNWD", np.nan)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path)



# ----------------------------------------------------------------------------------------------------------------
# Simulation


def _read_aorc(target: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    years = range(start.year, end.year + 1)
    frame = pd.concat([pd.read_parquet(CACHE / "aorc" / target / f"{y}.parquet") for y in years])
    return frame.loc[start:end]


def band_forcing(basin: str, hrus: HRUSet, start: pd.Timestamp, end: pd.Timestamp) -> tuple[xr.Dataset, np.ndarray]:
    """Per-HRU forcing (each aspect class gets its band's AORC mean) and the band elevations it represents."""
    raw = _read_aorc(basin, start, end)
    data = {}
    for aorc_name in AORC_VARS:
        cols = [f"{aorc_name}|{b}" for b in hrus.table["band_id"]]
        data[AORC_NAMES[aorc_name]] = (("time", "hru"), raw[cols].to_numpy(dtype=np.float64))
    ds = xr.Dataset(data, coords={"time": raw.index, "hru": hrus.ids})
    ds["air_temperature"] = ds["air_temperature"] - 273.15
    t = hrus.table
    band_elev = (t["elev_mean"] * t["area_km2"]).groupby(t["band_id"]).sum() / t["area_km2"].groupby(t["band_id"]).sum()
    return ds, band_elev.loc[t["band_id"]].to_numpy()


def basin_mean_forcing(forcing: xr.Dataset, hrus: HRUSet) -> pd.DataFrame:
    w = xr.DataArray(hrus.area_weights(), dims="hru", coords={"hru": hrus.ids})
    return (forcing * w).sum("hru").to_dataframe()


@dataclass
class Prepared:
    basin: str
    hrus: HRUSet
    f: dict
    times: pd.DatetimeIndex


def prepare(basin: str, start: pd.Timestamp, end: pd.Timestamp, params: SnowParams = NORTHEAST, mode: str = "band") -> Prepared:
    hrus = station_hrus().subset([basin]) if basin in STATIONS else basin_hrus(basin)
    if basin in STATIONS:
        raw = _read_aorc(basin, start, end)
        forcing = pd.DataFrame({AORC_NAMES[v]: raw[f"{v}|{basin}"].to_numpy(np.float64) for v in AORC_VARS}, index=raw.index)
        forcing["air_temperature"] -= 273.15
        f = prepare_forcing(forcing, hrus, params, forcing_elevation=station_cell_elevation(basin), radiation_label=AORC_RADIATION_LABEL)
    else:
        forcing, z = band_forcing(basin, hrus, start, end)
        if mode == "basin":
            f = prepare_forcing(basin_mean_forcing(forcing, hrus), hrus, params, radiation_label=AORC_RADIATION_LABEL)
        else:
            f = prepare_forcing(forcing, hrus, params, forcing_elevation=z, radiation_label=AORC_RADIATION_LABEL)
    return Prepared(basin, hrus, f, f["times"])


def station_cell_elevation(sid: str) -> float:
    """Mean terrain elevation of the AORC 1 km cell nearest to a station (the elevation its forcing represents)."""
    path = CACHE / "hrus" / "station_cells.json"
    cells = json.loads(path.read_text()) if path.exists() else {}
    if sid not in cells:
        _, lon, lat, _ = STATIONS[sid]
        lat_c = 20.0 + round((lat - 20.0) / (1 / 120)) / 120
        lon_c = -130.0 + round((lon + 130.0) / (1 / 120)) / 120
        dem = fetch_dem((lon_c - 1 / 240, lat_c - 1 / 240, lon_c + 1 / 240, lat_c + 1 / 240), zoom=12)
        cells[sid] = float(np.mean(dem.elev))
        path.write_text(json.dumps(cells))
    return cells[sid]


def simulate(p: Prepared, params: SnowParams) -> np.ndarray:
    """Kernel-only rerun on prepared forcing (phase re-split for the given parameters). Returns out[k, t, h]."""
    f = p.f
    fracs = np.ascontiguousarray(meteo.snow_fraction(f["ta"], f["wet_bulb"], params))
    hp = HruParams.build(params, p.hrus.table["lat"].to_numpy(), p.hrus.table["forest_frac"].to_numpy())
    out = np.zeros((N_OUT, *f["ta"].shape), dtype=np.float32)
    sw = f["sw"] if params.melt_shortwave == "actual" else np.ascontiguousarray(f["sw_clear"])
    snow17_kernel(f["ta"], f["px"], fracs, f["ea"], f["pa_mb"], f["wind"], sw, f["idn"], f["step_hours"],
                  hp.values, hp.adc, hp.flags, initial_state(p.hrus.n), out)  # fmt: skip
    return out


def daily_swe(p: Prepared, out: np.ndarray, hour: int = SWE_HOUR_UTC) -> pd.DataFrame:
    """Model SWE at `hour` UTC each day: basin mean and per band."""
    swe = pd.DataFrame(out[OUTPUTS.index("swe")], index=p.times, columns=p.hrus.ids)
    swe = swe[swe.index.hour == hour]
    swe.index = swe.index.normalize()
    t = p.hrus.table
    cols = {}
    w = p.hrus.area_weights()
    cols["basin"] = swe.to_numpy() @ w
    for band, rows in t.groupby("band_id"):
        wb = rows["area_km2"].to_numpy() / rows["area_km2"].sum()
        cols[band] = swe[rows["hru_id"]].to_numpy() @ wb
    return pd.DataFrame(cols, index=swe.index)


def snodas_daily(basin: str, hrus: HRUSet) -> pd.DataFrame:
    raw = pd.read_parquet(CACHE / "snodas" / f"{basin}.parquet")
    t = hrus.table
    band_area = t.groupby("band_id")["area_km2"].sum()
    cols = {b: raw[f"swe|{b}"] for b in band_area.index}
    frame = pd.DataFrame(cols)
    frame["basin"] = (frame[band_area.index] * (band_area / band_area.sum()).to_numpy()).sum(axis=1, min_count=len(band_area))
    frame.index = pd.DatetimeIndex(frame.index).normalize()
    return frame


def water_year(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.year + (idx.month >= 10)


# ----------------------------------------------------------------------------------------------------------------
# Metrics


def season_metrics(model: pd.Series, obs: pd.Series) -> pd.DataFrame:
    """Per water year: peak SWE, peak date, melt-out date, daily RMSE/bias/NSE over Nov-Jun."""
    df = pd.concat({"model": model, "obs": obs}, axis=1).dropna()
    df = df[df.index.month.isin([11, 12, 1, 2, 3, 4, 5, 6])]
    rows = []
    for wy, g in df.groupby(water_year(df.index)):
        if len(g) < 150 or g["obs"].max() < 5.0:
            continue
        row = {"wy": int(wy)}
        thresh = max(2.0, 0.1 * g["obs"].max())
        for k in ("model", "obs"):
            s = g[k]
            peak_day = s.idxmax()
            after = s.loc[peak_day:]
            gone = after[after < thresh]
            row[f"peak_{k}"] = float(s.max())
            row[f"peak_day_{k}"] = peak_day
            row[f"meltout_{k}"] = gone.index[0] if len(gone) else pd.NaT
            row[f"snow_days_{k}"] = int((s >= thresh).sum())
        err = g["model"] - g["obs"]
        row["rmse"] = float(np.sqrt(np.mean(err**2)))
        row["bias"] = float(err.mean())
        row["nse"] = float(1 - np.sum(err**2) / np.sum((g["obs"] - g["obs"].mean()) ** 2))
        row["r"] = float(np.corrcoef(g["model"], g["obs"])[0, 1])
        row["peak_bias_pct"] = 100 * (row["peak_model"] - row["peak_obs"]) / row["peak_obs"]
        row["peak_day_err"] = (row["peak_day_model"] - row["peak_day_obs"]).days
        row["meltout_err"] = (row["meltout_model"] - row["meltout_obs"]).days if pd.notna(row["meltout_model"]) and pd.notna(row["meltout_obs"]) else np.nan
        row["snow_days_err"] = row["snow_days_model"] - row["snow_days_obs"]
        rows.append(row)
    return pd.DataFrame(rows)


def _season_days(model: pd.Series, obs: pd.Series, wys) -> pd.DataFrame:
    df = pd.concat({"m": model, "o": obs}, axis=1).dropna()
    return df[np.isin(water_year(df.index), list(wys)) & df.index.month.isin([11, 12, 1, 2, 3, 4, 5])]


def pooled_nse(model: pd.Series, obs: pd.Series, wys) -> float:
    df = _season_days(model, obs, wys)
    return float(1 - np.sum((df["m"] - df["o"]) ** 2) / np.sum((df["o"] - df["o"].mean()) ** 2))


def pooled_kge(model: pd.Series, obs: pd.Series, wys) -> float:
    """Kling-Gupta efficiency of daily SWE (Nov-May): penalizes timing, damped variability and volume bias."""
    df = _season_days(model, obs, wys)
    r = np.corrcoef(df["m"], df["o"])[0, 1]
    alpha = df["m"].std() / df["o"].std()
    beta = df["m"].mean() / df["o"].mean()
    return float(1 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2))


# ----------------------------------------------------------------------------------------------------------------
# Calibration

# Two process parameters are fixed because SNODAS basin SWE cannot identify them (the sweeps show flat or weak
# responses): the Hock radiation factor rf, which spreads melt by aspect and shading, and the rain-on-snow wind
# function, set from bulk transfer theory (~0.03 mm/mb/6h per m/s for neutral open snow, reduced for stable air).
FLOWCAST_SPACE = {"scf": (0.7, 1.5), "tf": (0.0, 0.4), "mbase": (-1.0, 3.0), "tw_mid": (-1.0, 2.5), "nmf": (0.005, 0.4)}
SWEEPS = {"rf": (0.0, 0.0001, 0.0002, 0.0004, 0.0008), "wind_function": (0.002, 0.005, 0.01, 0.02, 0.03, 0.05)}
CLASSIC_SPACE = {"scf": (0.7, 1.5), "mfmax": (0.3, 3.0), "mfmin": (0.02, 2.0), "mbase": (-1.0, 3.0), "uadj": (0.01, 0.5), "pxtemp": (-1.0, 3.0), "nmf": (0.01, 0.4)}


def _apply(base: SnowParams, names: list[str], x: np.ndarray) -> SnowParams:
    changes = dict(zip(names, (float(v) for v in x)))
    if "tw_mid" in changes:
        mid = changes.pop("tw_mid")
        changes["tw_snow"], changes["tw_rain"] = mid - 1.0, mid + 1.0
    return base.replace(**changes)


class Stack:
    """Several prepared basins on a common time axis, run as one kernel call (HRUs in parallel)."""

    def __init__(self, prepared: list[Prepared]):
        self.prepared = prepared
        self.times = prepared[0].times
        self.f = {k: np.ascontiguousarray(np.concatenate([p.f[k] for p in prepared], axis=1))
                  for k in ("ta", "px", "ea", "pa_mb", "wind", "sw", "sw_clear", "wet_bulb")}  # fmt: skip
        self.idn = prepared[0].f["idn"]
        self.step = prepared[0].f["step_hours"]
        self.lat = np.concatenate([p.hrus.table["lat"].to_numpy() for p in prepared])
        self.forest = np.concatenate([p.hrus.table["forest_frac"].to_numpy() for p in prepared])
        self.offsets = np.cumsum([0] + [p.hrus.n for p in prepared])
        at_hour = self.times.hour == SWE_HOUR_UTC
        self.daily_rows = np.where(at_hour)[0]
        self.days = self.times[at_hour].normalize()

    def basin_daily_swe(self, params: SnowParams) -> list[pd.Series]:
        fracs = np.ascontiguousarray(meteo.snow_fraction(self.f["ta"], self.f["wet_bulb"], params))
        hp = HruParams.build(params, self.lat, self.forest)
        nt, nh = self.f["ta"].shape
        out = np.zeros((N_OUT, nt, nh), dtype=np.float32)
        sw = self.f["sw"] if params.melt_shortwave == "actual" else self.f["sw_clear"]
        snow17_kernel(self.f["ta"], self.f["px"], fracs, self.f["ea"], self.f["pa_mb"], self.f["wind"], sw,
                      self.idn, self.step, hp.values, hp.adc, hp.flags, initial_state(nh), out)  # fmt: skip
        swe = out[OUTPUTS.index("swe")][self.daily_rows]
        series = []
        for k, p in enumerate(self.prepared):
            sl = slice(self.offsets[k], self.offsets[k + 1])
            series.append(pd.Series(swe[:, sl] @ p.hrus.area_weights(), index=self.days))
        return series


def calibrate(base: SnowParams, space: dict, label: str, maxiter: int = 30, seed: int = 1) -> tuple[SnowParams, float]:
    start, end = water_year_bounds(CAL_WYS[0])[0], water_year_bounds(CAL_WYS[-1])[1]
    cal_basins = [b for b, m in BASINS.items() if not m.holdout]
    stack = Stack([prepare(b, start, end, base) for b in cal_basins])
    obs = [snodas_daily(p.basin, p.hrus)["basin"] for p in stack.prepared]
    names = list(space)

    def loss(x):
        sims = stack.basin_daily_swe(_apply(base, names, x))
        return -float(np.mean([pooled_kge(m, o, CAL_WYS) for m, o in zip(sims, obs)]))

    res = optimize.differential_evolution(loss, list(space.values()), maxiter=maxiter, popsize=10, seed=seed, tol=1e-4, polish=True)
    params = _apply(base, names, res.x)
    print(f"{label}: mean calibration KGE {-res.fun:.3f} with {dict(zip(names, np.round(res.x, 4)))}", flush=True)
    return params, -float(res.fun)



def sensitivity_sweeps(params: SnowParams) -> pd.DataFrame:
    """Calibration KGE/NSE with each fixed process parameter set to each SWEEPS value (the fitted ones re-fitted)."""
    start, end = water_year_bounds(CAL_WYS[0])[0], water_year_bounds(CAL_WYS[-1])[1]
    stack = Stack([prepare(b, start, end, params) for b, m in BASINS.items() if not m.holdout])
    obs = [snodas_daily(p.basin, p.hrus)["basin"] for p in stack.prepared]
    names = list(FLOWCAST_SPACE)
    x0 = [params.scf, params.tf, params.mbase, 0.5 * (params.tw_snow + params.tw_rain), params.nmf]
    rows = []
    for name, value in [(n, v) for n, values in SWEEPS.items() for v in values]:
        base = params.replace(**{name: value})

        def loss(x):
            sims = stack.basin_daily_swe(_apply(base, names, x))
            return -float(np.mean([pooled_kge(m, o, CAL_WYS) for m, o in zip(sims, obs)]))

        res = optimize.minimize(loss, x0, method="Nelder-Mead", options={"maxiter": 300, "xatol": 1e-3, "fatol": 1e-4})
        sims = stack.basin_daily_swe(_apply(base, names, res.x))
        nse = float(np.mean([pooled_nse(m, o, CAL_WYS) for m, o in zip(sims, obs)]))
        rows.append({"parameter": name, "value": value, "calibration_kge": -res.fun, "calibration_nse": nse, **dict(zip(names, res.x))})
        print(f"{name}={value}: KGE {-res.fun:.4f} NSE {nse:.4f}", flush=True)
    return pd.DataFrame(rows)

# ----------------------------------------------------------------------------------------------------------------
# Evaluation

VARIANTS = {
    "flowcast": "flowcast defaults (Hock melt with terrain shortwave, wet-bulb split, humidity/wind rain-on-snow, canopy), per-band forcing",
    "flowcast_basin_forcing": "same parameters, basin-mean forcing lapsed to bands (the dataset-step path)",
    "flowcast_no_terrain": "same parameters, flat terrain (no slope/aspect/shading)",
    "classic": "classic SNOW-17 (seasonal melt factor, air-temperature split, 90% RH rain-on-snow), recalibrated the same way",
}


def _params_path(name: str) -> Path:
    return RESULTS / f"params_{name}.json"


def load_or_calibrate(name: str) -> SnowParams:
    path = _params_path(name)
    if path.exists():
        return SnowParams.from_dict(json.loads(path.read_text())["params"])
    base, space = (NORTHEAST, FLOWCAST_SPACE) if name == "flowcast" else (CLASSIC, CLASSIC_SPACE)
    params, score = calibrate(base, space, name)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"calibration_kge": score, "calibration_wys": [CAL_WYS[0], CAL_WYS[-1]],
                                "params": params.to_dict()}, indent=2))  # fmt: skip
    return params


def _flat(hrus: HRUSet) -> HRUSet:
    return HRUSet(hrus.table.assign(svf=1.0), terrain.flat_lut(hrus.n), hrus.geometry, hrus.meta)


def run_variant(basin: str, variant: str, params: dict[str, SnowParams]) -> tuple[Prepared, np.ndarray]:
    start, end = water_year_bounds(FIRST_WY)[0], water_year_bounds(LAST_WY)[1]
    if variant == "classic":
        p = prepare(basin, start, end, params["classic"])
        return p, simulate(p, params["classic"])
    par = params["flowcast"]
    if variant == "flowcast_basin_forcing":
        p = prepare(basin, start, end, par, mode="basin")
    elif variant == "flowcast_no_terrain":
        p = prepare(basin, start, end, par)
        flat = _flat(p.hrus)
        forcing, z = band_forcing(basin, flat, start, end)
        p = Prepared(basin, flat, prepare_forcing(forcing, flat, par, forcing_elevation=z, radiation_label=AORC_RADIATION_LABEL), p.times)
    else:
        p = prepare(basin, start, end, par)
    return p, simulate(p, par)


def evaluate() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    params = {"flowcast": load_or_calibrate("flowcast"), "classic": load_or_calibrate("classic")}
    seasons, pooled, keep = [], [], {}
    for basin, meta in BASINS.items():
        for variant in VARIANTS:
            p, out = run_variant(basin, variant, params)
            model = daily_swe(p, out)
            obs = snodas_daily(basin, p.hrus)
            m = season_metrics(model["basin"], obs["basin"])
            m.insert(0, "variant", variant)
            m.insert(0, "basin", basin)
            seasons.append(m)
            for period, wys in (("calibration", CAL_WYS), ("validation", VAL_WYS)):
                pooled.append({"basin": basin, "variant": variant, "period": period, "holdout": meta.holdout, "region": meta.region,
                               "nse": pooled_nse(model["basin"], obs["basin"], wys), "kge": pooled_kge(model["basin"], obs["basin"], wys)})  # fmt: skip
            if variant in ("flowcast", "classic"):
                keep[(basin, variant)] = (p, out, model, obs)
        print(f"evaluated {basin}", flush=True)
    seasons = pd.concat(seasons, ignore_index=True)
    seasons["period"] = np.where(seasons["wy"].isin(list(CAL_WYS)), "calibration", "validation")
    seasons["holdout"] = seasons["basin"].map(lambda b: BASINS[b].holdout)
    seasons.to_csv(RESULTS / "season_metrics.csv", index=False)
    pooled = pd.DataFrame(pooled)
    pooled.to_csv(RESULTS / "pooled_nse.csv", index=False)
    summary = summarize(seasons, pooled)
    summary.to_csv(RESULTS / "summary.csv", index=False)
    stations = evaluate_stations(params["flowcast"])
    stations.to_csv(RESULTS / "stations.csv", index=False)
    (RESULTS / "summary.md").write_text(summary_markdown(summary, stations))
    make_plots(keep, seasons, params)
    print((RESULTS / "summary.md").read_text())


def summarize(seasons: pd.DataFrame, pooled: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (variant, period, holdout), g in seasons.groupby(["variant", "period", "holdout"]):
        pn = pooled[(pooled["variant"] == variant) & (pooled["period"] == period) & (pooled["holdout"] == holdout)]
        rows.append({
            "variant": variant, "period": period, "basins": "holdout" if holdout else "calibration basins",
            "n_basins": g["basin"].nunique(), "n_seasons": len(g),
            "median_nse": pn["nse"].median(),
            "median_kge": pn["kge"].median(),
            "median_season_r": g["r"].median(),
            "peak_within_7d_pct": 100 * (g["peak_day_err"].abs() <= 7).mean(),
            "median_peak_bias_pct": g["peak_bias_pct"].median(),
            "median_abs_peak_err_pct": g["peak_bias_pct"].abs().median(),
            "mae_peak_date_days": g["peak_day_err"].abs().median(),
            "median_meltout_err_days": g["meltout_err"].median(),
            "mae_meltout_days": g["meltout_err"].abs().median(),
            "rmse_mm": g["rmse"].median(),
        })  # fmt: skip
    return pd.DataFrame(rows)


def evaluate_stations(params: SnowParams) -> pd.DataFrame:
    start, end = water_year_bounds(FIRST_WY)[0], water_year_bounds(LAST_WY)[1]
    rows = []
    for sid, (name, _, _, z) in STATIONS.items():
        p = prepare(sid, start, end, params)
        out = simulate(p, params)
        swe = pd.Series(out[OUTPUTS.index("swe")][:, 0], index=p.times)
        model = swe[swe.index.hour == 12]
        model.index = model.index.normalize()
        obs = pd.read_parquet(CACHE / "ghcn" / f"{sid}.parquet")["wesd_mm"].dropna()
        snodas = pd.read_parquet(CACHE / "snodas" / f"{sid}.parquet")["swe"]
        snodas.index = pd.DatetimeIndex(snodas.index).normalize()
        df = pd.concat({"obs": obs, "model": model, "snodas": snodas}, axis=1).dropna()
        df = df[df.index.month.isin([11, 12, 1, 2, 3, 4, 5])]
        if len(df) < 10:
            continue
        row = {"station": sid, "name": name, "elev_m": z, "n_obs": len(df), "n_obs_snow": int((df["obs"] > 0).sum()), "mean_obs": df["obs"].mean()}
        for k in ("model", "snodas"):
            err = df[k] - df["obs"]
            row[f"{k}_bias"] = err.mean()
            row[f"{k}_rmse"] = float(np.sqrt((err**2).mean()))
            row[f"{k}_r"] = df[[k, "obs"]].corr().iloc[0, 1]
            row[f"{k}_hit_rate"] = float(((df[k] > 1) == (df["obs"] > 1)).mean())
        rows.append(row)
        df.to_csv(CACHE / "ghcn" / f"{sid}_paired.csv")
    return pd.DataFrame(rows)


def summary_markdown(summary: pd.DataFrame, stations: pd.DataFrame) -> str:
    cols = ["variant", "period", "basins", "n_basins", "n_seasons", "median_nse", "median_kge", "median_season_r", "peak_within_7d_pct", "median_abs_peak_err_pct",
            "median_peak_bias_pct", "mae_peak_date_days", "median_meltout_err_days", "mae_meltout_days", "rmse_mm"]  # fmt: skip
    lines = ["# Snow module validation vs SNODAS", "", f"Calibration WY{CAL_WYS[0]}-{CAL_WYS[-1]}, validation WY{VAL_WYS[0]}-{VAL_WYS[-1]}.", ""]
    lines += [f"- `{k}`: {v}" for k, v in VARIANTS.items()] + [""]
    lines.append(summary[cols].round(2).to_markdown(index=False))
    lines += ["", "## Stations (GHCN-Daily WESD, Nov-May)", ""]
    lines.append(stations.round(2).to_markdown(index=False))
    return "\n".join(lines) + "\n"



# ----------------------------------------------------------------------------------------------------------------
# Plots

COLORS = {"model": "#1f6feb", "classic": "#d29922", "obs": "#222222", "north": "#1f6feb", "south": "#e5534b"}


def _wy_slice(frame, wy0: int, wy1: int):
    return frame.loc[f"{wy0 - 1}-10-01":f"{wy1}-07-01"]


def make_plots(keep: dict, seasons: pd.DataFrame, params: dict[str, SnowParams]) -> None:
    plot_timeseries(keep)
    plot_scatter(seasons)
    plot_bands_aspect(keep)
    plot_stations()
    plot_rain_on_snow(params)
    plot_map(keep)


def plot_timeseries(keep: dict) -> None:
    basins = ["USGS-01423000", "USGS-01413500", "USGS-01420500", "USGS-01054200"]
    fig, axes = plt.subplots(len(basins), 1, figsize=(12, 2.6 * len(basins)), sharex=True)
    for ax, b in zip(axes, basins):
        _, _, model, obs = keep[(b, "flowcast")]
        classic = keep[(b, "classic")][2]
        ax.fill_between(_wy_slice(obs, 2016, 2025).index, _wy_slice(obs, 2016, 2025)["basin"], color="#bbbbbb", label="SNODAS", lw=0)
        ax.plot(_wy_slice(model, 2016, 2025)["basin"], color=COLORS["model"], lw=1.2, label="flowcast snow module")
        ax.plot(_wy_slice(classic, 2016, 2025)["basin"], color=COLORS["classic"], lw=0.9, alpha=0.8, label="classic SNOW-17")
        tag = " (held out)" if BASINS[b].holdout else ""
        ax.set_title(f"{BASINS[b].name}{tag}", fontsize=10, loc="left")
        ax.set_ylabel("basin SWE (mm)")
    axes[0].legend(loc="upper right", fontsize=8, ncol=3)
    axes[-1].set_xlabel("validation water years 2016-2025")
    fig.tight_layout()
    fig.savefig(RESULTS / "swe_timeseries.png", dpi=130)
    plt.close(fig)


def plot_scatter(seasons: pd.DataFrame) -> None:
    s = seasons[seasons["variant"] == "flowcast"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    groups = [("calibration", False, "#8b949e", "calibration basins, WY2007-15"),
              ("validation", False, COLORS["model"], "calibration basins, WY2016-25"),
              ("validation", True, COLORS["south"], "held-out basins, WY2016-25")]  # fmt: skip
    for period, holdout, color, label in groups:
        g = s[(s["period"] == period) & (s["holdout"] == holdout)]
        axes[0].scatter(g["peak_obs"], g["peak_model"], s=18, color=color, label=label, alpha=0.8)
        mo = g.dropna(subset=["meltout_err"])
        axes[1].scatter(mo["meltout_obs"].map(lambda d: d.dayofyear if d.month < 10 else d.dayofyear - 365),
                        mo["meltout_model"].map(lambda d: d.dayofyear if d.month < 10 else d.dayofyear - 365),
                        s=18, color=color, alpha=0.8, label=label)  # fmt: skip
    lim = max(s["peak_obs"].max(), s["peak_model"].max()) * 1.05
    axes[0].plot([0, lim], [0, lim], color="k", lw=0.8)
    axes[0].set(xlabel="SNODAS peak basin SWE (mm)", ylabel="model peak basin SWE (mm)", xlim=(0, lim), ylim=(0, lim), title="Peak SWE per basin-season")
    lo, hi = axes[1].get_xlim()
    lo, hi = min(lo, axes[1].get_ylim()[0]), max(hi, axes[1].get_ylim()[1])
    axes[1].plot([lo, hi], [lo, hi], color="k", lw=0.8)
    axes[1].set(xlabel="SNODAS melt-out (day of year)", ylabel="model melt-out (day of year)", title="Melt-out date per basin-season")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(RESULTS / "peak_meltout_scatter.png", dpi=130)
    plt.close(fig)


def plot_bands_aspect(keep: dict, basin: str = "USGS-01423000", wy: int = 2014) -> None:
    p, out, model, obs = keep[(basin, "flowcast")]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2))
    bands = sorted(c for c in model.columns if c != "basin")
    cmap = plt.get_cmap("viridis")
    for k, band in enumerate(bands):
        c = cmap(k / max(len(bands) - 1, 1))
        z = p.hrus.table.loc[p.hrus.table["band_id"] == band, "elev_mean"].mean()
        axes[0].plot(_wy_slice(model, wy, wy)[band], color=c, lw=1.4, label=f"band {k + 1} (~{z:.0f} m) model")
        axes[0].plot(_wy_slice(obs, wy, wy)[band], color=c, lw=1.0, ls="--")
    axes[0].set(title=f"{BASINS[basin].name.split(' (')[0]}: SWE by elevation band, WY{wy} (dashed = SNODAS)", ylabel="SWE (mm)")
    axes[0].title.set_fontsize(9)
    axes[0].legend(fontsize=7)
    swe = pd.DataFrame(out[OUTPUTS.index("swe")], index=p.times, columns=p.hrus.ids)
    sw = pd.DataFrame(out[OUTPUTS.index("melt")], index=p.times, columns=p.hrus.ids)
    top = bands[-1].split(":b")[1]
    n_id, s_id = f"{basin}:b{top}n", f"{basin}:b{top}s"
    ax = axes[1]
    sl = slice(f"{wy}-02-15", f"{wy}-05-01")
    ax.plot(swe.loc[sl, n_id], color=COLORS["north"], label="north-facing HRU SWE")
    ax.plot(swe.loc[sl, s_id], color=COLORS["south"], label="south-facing HRU SWE")
    ax2 = ax.twinx()
    daily_melt = sw.loc[sl, [n_id, s_id]].resample("D").sum()
    ax2.bar(daily_melt.index - pd.Timedelta(hours=5), daily_melt[n_id], width=0.4, color=COLORS["north"], alpha=0.35)
    ax2.bar(daily_melt.index + pd.Timedelta(hours=5), daily_melt[s_id], width=0.4, color=COLORS["south"], alpha=0.35)
    ax2.set_ylabel("daily melt (mm)")
    ax.set(title=f"Top band, north vs south aspect (terrain-corrected shortwave), spring {wy}", ylabel="SWE (mm)")
    ax.title.set_fontsize(9)
    ax.legend(fontsize=8, loc="upper right")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(RESULTS / "bands_aspect.png", dpi=130)
    plt.close(fig)


def plot_stations() -> None:
    picks = [sid for sid in ("US1NYDL0025", "US1NYUL0028", "US1NYSV0006") if (CACHE / "ghcn" / f"{sid}_paired.csv").exists()]
    fig, axes = plt.subplots(len(picks), 1, figsize=(12, 2.6 * len(picks)), squeeze=False)
    for ax, sid in zip(axes[:, 0], picks):
        df = pd.read_csv(CACHE / "ghcn" / f"{sid}_paired.csv", index_col=0, parse_dates=True)
        wys = water_year(df[df["obs"] > 0].index)
        wy = pd.Series(wys).value_counts().idxmax()
        g = _wy_slice(df, wy, wy)
        ax.plot(g.index, g["model"], "o-", ms=3, color=COLORS["model"], lw=0.8, label="model (station HRU, AORC cell)")
        ax.plot(g.index, g["snodas"], "s-", ms=3, color="#8b949e", lw=0.8, label="SNODAS cell")
        ax.plot(g.index, g["obs"], "k*", ms=7, label="observed WESD")
        ax.set_title(f"{STATIONS[sid][0]} ({STATIONS[sid][3]:.0f} m), WY{wy}", fontsize=10, loc="left")
        ax.set_ylabel("SWE (mm)")
    axes[0, 0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(RESULTS / "stations.png", dpi=130)
    plt.close(fig)


def plot_rain_on_snow(params: dict[str, SnowParams], basin: str = "USGS-01423000") -> None:
    par = params["flowcast"]
    start, end = water_year_bounds(FIRST_WY)[0], water_year_bounds(LAST_WY)[1]
    p = prepare(basin, start, end, par)
    out = simulate(p, par)
    w = p.hrus.area_weights()
    ros = pd.Series(out[OUTPUTS.index("ros_melt")] @ w, index=p.times)
    peak = ros.rolling(24).sum().idxmax()
    sl = slice(peak - pd.Timedelta(days=3), peak + pd.Timedelta(days=2))
    classic_ros = simulate(p, par.replace(rain_on_snow=0))
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    rain = pd.Series(out[OUTPUTS.index("rainfall")] @ w, index=p.times)[sl]
    snowf = pd.Series(out[OUTPUTS.index("snowfall")] @ w, index=p.times)[sl]
    axes[0].bar(rain.index, rain, width=1 / 24, color="#1f6feb", label="rain")
    axes[0].bar(snowf.index, snowf, width=1 / 24, bottom=rain, color="#8b949e", label="snow")
    ax0 = axes[0].twinx()
    ta = pd.Series(p.f["ta"] @ w, index=p.times)[sl]
    td = pd.Series(meteo.dewpoint_from_vapor_pressure(p.f["ea"]) @ w, index=p.times)[sl]
    wind = pd.Series(p.f["wind"] @ w, index=p.times)[sl]
    ax0.plot(ta, color="#e5534b", label="air temp")
    ax0.plot(td, color="#e5534b", ls=":", label="dewpoint")
    ax0.plot(wind, color="#57606a", lw=0.8, label="wind (m/s)")
    ax0.set_ylabel("degC | m/s")
    axes[0].set_ylabel("precip (mm/h)")
    axes[0].legend(loc="upper left", fontsize=8)
    ax0.legend(loc="upper right", fontsize=8)
    for arr, color, label in ((out, COLORS["model"], "humidity + wind rain-on-snow"), (classic_ros, COLORS["classic"], "classic (90% RH, constant wind)")):
        axes[1].plot(pd.Series(arr[OUTPUTS.index("rain_plus_melt")] @ w, index=p.times)[sl], color=color, label=label)
        axes[2].plot(pd.Series(arr[OUTPUTS.index("swe")] @ w, index=p.times)[sl], color=color, label=label)
    obs = snodas_daily(basin, p.hrus)["basin"]
    axes[2].plot(obs.index + pd.Timedelta(hours=SWE_HOUR_UTC), obs, "ko", ms=5, label="SNODAS")
    axes[2].set_xlim(sl.start, sl.stop)
    axes[1].set_ylabel("rain + melt (mm/h)")
    axes[2].set_ylabel("basin SWE (mm)")
    axes[1].legend(fontsize=8)
    axes[2].legend(fontsize=8)
    axes[0].set_title(f"Largest modeled rain-on-snow day, {BASINS[basin].name.split(' (')[0]}, {peak:%Y-%m-%d}", fontsize=10, loc="left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(RESULTS / "rain_on_snow_event.png", dpi=130)
    plt.close(fig)


def plot_map(keep: dict, day: str = "2014-03-05", melt_window: tuple[str, str] = ("2014-04-06", "2014-04-13")) -> None:
    catskills = [b for b, m in BASINS.items() if m.cluster == "catskills"]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.4))
    panels = [("model SWE (mm)", "swe"), ("SNODAS SWE (mm)", "obs"), (f"model rain + melt {melt_window[0]} to {melt_window[1]} (mm)", "raim")]
    values = {k: [] for _, k in panels}
    shapes = []
    for b in catskills:
        p, out, model, obs = keep[(b, "flowcast")]
        raim = pd.DataFrame(out[OUTPUTS.index("rain_plus_melt")], index=p.times, columns=p.hrus.ids).loc[melt_window[0]:melt_window[1]].sum()
        for feat in p.hrus.geometry["features"]:
            band = feat["properties"]["band_id"]
            rows = p.hrus.table[p.hrus.table["band_id"] == band]
            wb = rows["area_km2"].to_numpy() / rows["area_km2"].sum()
            shapes.append(feat["geometry"])
            values["swe"].append(model.loc[day, band])
            values["obs"].append(obs.loc[day, band])
            values["raim"].append(float(raim[rows["hru_id"]].to_numpy() @ wb))
    geoms = [shapely.geometry.shape(g) for g in shapes]
    for ax, (title, key) in zip(axes, panels):
        v = np.asarray(values[key])
        vmax = max(np.nanmax(values["swe"]), np.nanmax(values["obs"])) if key in ("swe", "obs") else np.nanmax(v)
        cmap = plt.get_cmap("Blues" if key != "raim" else "PuBu")
        for g, val in zip(geoms, v):
            polys = list(g.geoms) if hasattr(g, "geoms") else [g]
            for poly in polys:
                x, y = poly.exterior.xy
                ax.fill(x, y, color=cmap(val / vmax if vmax > 0 else 0), lw=0)
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, vmax))
        fig.colorbar(sm, ax=ax, shrink=0.8)
        ax.set_title(title if key == "raim" else f"{title}, {day}", fontsize=10)
        ax.set_aspect(1 / np.cos(np.radians(42.1)))
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle("Catskills basins by elevation band (Cannonsville, Pepacton, Beaver Kill, Neversink, Schoharie, Esopus)", fontsize=10)
    fig.tight_layout()
    fig.savefig(RESULTS / "map_bands.png", dpi=130)
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-snow-validate", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--years", default=f"{FIRST_WY - 1}-{LAST_WY}")
    f.add_argument("--clusters", nargs="*")
    f.add_argument("--what", nargs="*", default=["hrus", "ghcn", "snodas", "aorc"])
    c = sub.add_parser("calibrate")
    c.add_argument("--models", nargs="*", default=["flowcast", "classic"])
    sub.add_parser("sweep")
    sub.add_parser("evaluate")
    args = parser.parse_args(argv)
    if args.cmd == "calibrate":
        for name in args.models:
            _params_path(name).unlink(missing_ok=True)
            load_or_calibrate(name)
    if args.cmd == "sweep":
        sweep = sensitivity_sweeps(load_or_calibrate("flowcast"))
        RESULTS.mkdir(parents=True, exist_ok=True)
        sweep.round(5).to_csv(RESULTS / "sensitivity_sweeps.csv", index=False)
    if args.cmd == "evaluate":
        evaluate()
    if args.cmd == "fetch":
        if "hrus" in args.what:
            for b in BASINS:
                basin_hrus(b)
            station_hrus()
        if "ghcn" in args.what:
            fetch_ghcn()
        if "snodas" in args.what:
            fetch_snodas()
        if "aorc" in args.what:
            y0, y1 = (int(x) for x in args.years.split("-"))
            fetch_aorc(list(range(y0, y1 + 1)), args.clusters)


if __name__ == "__main__":
    main()
