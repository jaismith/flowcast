"""Build the landing-page data in `public/data/` from flowcast's saved hindcasts, the training cube and live USGS data.

Forecasts are validation-year (WY2021-2022) hindcasts only: the frozen test years (WY2023+) are never read. Every
forecast timestamp is checked against `VALIDATION_END`, and the cube read is `trainval.zarr`, which stops there too.

- Flow: the final three-seed SNODAS model, pooled from the saved CMAL mixtures exactly as `mixture-ensemble`
  (PR #47) does it (4 draws per seed and GEFS member, 132 samples), then calibrated with the PR #51 standard-path
  conditional stretch (`internal/calibration/standard-path/params/flow`). Each issue uses the fit that held out its
  own water year.
- Water temperature: v1 seeds 42 + 44 (40 coherent members) with the PR #51 `temp-score` calibration: a per-lead
  offset and spread factor for hourly values, and the warm-up offset for daily highs.
- Skill: fair ensemble CRPS (`flowcast_eval.metrics`) against persistence at the 00/06/12/18Z cycle issues, and
  against MARFC's RVF bulletins at their own issue times at Callicoon.
- Observations from any period are fine: the training cube (WY2001-2022) for climatology, live USGS for "now".

Run `python scripts/build_data.py --help`; steps cache their S3 downloads in `--cache`.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import time
import zlib
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import requests
import s3fs
import xarray as xr
from shapely.geometry import Point, shape

from flowcast_eval.archive import read_archive
from flowcast_eval.baselines.persistence import last_available
from flowcast_eval.metrics import crps_ensemble
from flowcast_eval.pairs import lookup_obs
from flowcast_eval.protocol import lead_bin
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.usgs.client import WaterDataClient

from sites import SITES

TRAINING_BUCKET = "flowcast-training-257129854363"
DATASET_BUCKET = "flowcast-dataset-257129854363"
CUBE = f"{DATASET_BUCKET}/v1.3/full/trainval.zarr"
SELECTION_KEY = "work/meta/selection.parquet"
FLOW_RUNS = {
    "s42": "runs/full-v2-snodas-s42-0928-2320",
    "s43": "runs/full-v2-snodas-s43-0930-0505-pc",
    "s44": "runs/full-v2-snodas-s44-0930-0505-pc",
}
TEMP_RUNS = {"s42": "runs/temp-v1b-s42-0928-1435", "s44": "runs/temp-v1b-s44-0928-1437"}
VALIDATION_START = pd.Timestamp("2020-10-01T00:00", tz="UTC")
VALIDATION_END = pd.Timestamp("2022-09-30T23:00", tz="UTC")
CLIMATOLOGY_START = pd.Timestamp("2000-10-01T00:00", tz="UTC")
M3S_TO_CFS = 35.314666721
DRAWS_PER_MEMBER = 4
QUANTS = np.array([0.05, 0.25, 0.5, 0.75, 0.95])
MARFC_LEADS = (6.0, 12.0, 18.0, 24.0, 36.0, 48.0, 60.0, 72.0)
LOCAL_TZ = "America/New_York"
WARM_MONTHS = (5, 6, 7, 8, 9)
WARMUP_CLIP = 12.0
GEFS_LATENCY_H = 6.0
GEFS_TEMP_MEMBERS = 5
MIN_HOURS_PER_DAY = 20
BOOTSTRAP_DRAWS = 1000
# Median CRPS skill vs persistence over 552 basins, calibrated three-seed pool (calibration-results.md section 3).
NATIONAL_FLOW_SKILL = {1: 0.339, 6: 0.556, 12: 0.599, 24: 0.636, 48: 0.644, 72: 0.629, 120: 0.576, 168: 0.523}
NLDI = "https://api.water.usgs.gov/nldi/linked-data"
WFS = "https://api.water.usgs.gov/geoserver/wmadata/ows"
NID = "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/National_Inventory_of_Dams_Public_Service/FeatureServer/0/query"

ROOT = Path(__file__).resolve().parents[1]
STORE = Path("/cursor/stores/bc-39f2333f-ee6e-47c7-a93a-62820868bcfa")
DEFAULT_CAL = STORE / "internal/calibration/standard-path/params"

s3 = boto3.client("s3")
http = requests.Session()
http.headers["User-Agent"] = "flowcast-landing-prototype (github.com/jaismith/flowcast)"


# ---------------------------------------------------------------------------------------------- helpers


def sig(a, digits: int = 3):
    """Round to significant digits (NaN -> None) for compact JSON."""
    a = np.asarray(a, float)
    out = np.full(a.shape, np.nan)
    ok = np.isfinite(a) & (a != 0)
    mag = np.floor(np.log10(np.abs(a[ok])))
    scale = 10.0 ** (digits - 1 - mag)
    out[ok] = np.round(a[ok] * scale) / scale
    out[np.isfinite(a) & (a == 0)] = 0.0
    return out


def jsonable(a, digits: int | None = 3, decimals: int | None = None):
    a = np.asarray(a, float)
    a = np.round(a, decimals) if decimals is not None else sig(a, digits)
    return [None if not math.isfinite(v) else (int(v) if v == int(v) and abs(v) < 1e15 else float(v)) for v in a.ravel().tolist()]


def seconds(t) -> np.ndarray:
    """Unix seconds of tz-aware times at any datetime64 resolution (the archive is ms, the hindcasts ns)."""
    return ((pd.DatetimeIndex(t).tz_convert("UTC") - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)


def epoch(t) -> list[int]:
    return seconds(t).tolist()


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} kB)", flush=True)


def check_validation(times, what: str) -> None:
    t = pd.DatetimeIndex(times)
    if len(t) and (t.min() < VALIDATION_START or t.max() > VALIDATION_END):
        raise RuntimeError(f"{what}: times outside WY2021-2022 ({t.min()} .. {t.max()}); the frozen test must stay unread")


def local_days(hour_ending: pd.DatetimeIndex) -> np.ndarray:
    """Local calendar date of hour-ending UTC values (each covers the hour before), as datetime64[D]."""
    return (pd.DatetimeIndex(hour_ending) - pd.Timedelta(hours=1)).tz_convert(LOCAL_TZ).tz_localize(None).normalize().values.astype("datetime64[D]")


def daily_max(hourly: pd.Series) -> pd.Series:
    s = hourly.dropna()
    g = s.groupby(local_days(s.index))
    out = g.max()[g.count() >= MIN_HOURS_PER_DAY]
    return out.set_axis(pd.DatetimeIndex(out.index).tz_localize("UTC"))


def water_year(t) -> np.ndarray:
    t = pd.DatetimeIndex(t)
    return (t.year + (t.month >= 10)).to_numpy()


def get_json(url: str, **params):
    for attempt in range(4):
        r = http.get(url, params=params, timeout=180)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 2))
            continue
        r.raise_for_status()
    r.raise_for_status()


def round_coords(coords, nd: int = 5):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [round_coords(c, nd) for c in coords]


# ---------------------------------------------------------------------------------------------- cube


def open_cube() -> xr.Dataset:
    return xr.open_zarr(s3fs.S3Map(CUBE, s3=s3fs.S3FileSystem()))


def selection() -> pd.DataFrame:
    sel = pd.read_parquet(io.BytesIO(s3.get_object(Bucket=DATASET_BUCKET, Key=SELECTION_KEY)["Body"].read()))
    sel.index = sel.index.astype(str)
    return sel


def series(cube: xr.Dataset, var: str, basin: str, start=CLIMATOLOGY_START, end=VALIDATION_END) -> pd.Series:
    da = cube[var].sel(basin=basin, time=slice(start.tz_convert(None), end.tz_convert(None))).load()
    return pd.Series(da.values.astype(float), index=pd.DatetimeIndex(da.time.values).tz_localize("UTC"))


# ---------------------------------------------------------------------------------------------- flow


def sample_cmal(pi, mu, b, tau, n, rng):
    """`mixture-ensemble`'s sampler (PR #47): n draws per row from a CMAL (asymmetric Laplace) mixture."""
    rows, k = pi.shape
    cum = np.cumsum(pi / pi.sum(axis=1, keepdims=True), axis=1)
    comp = (rng.random((rows, n))[:, :, None] > cum[:, None, :]).sum(axis=2).clip(max=k - 1)
    m, s, t = (np.take_along_axis(a, comp, axis=1) for a in (mu, b, tau))
    u = rng.uniform(1e-6, 1 - 1e-6, (rows, n))
    return np.where(u < t, m + s * np.log(u / t) / (1 - t), m - s * np.log((1 - u) / (1 - t)) / t)


def download(key: str, local: Path) -> Path:
    if not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(TRAINING_BUCKET, key, str(local))
    return local


def flow_pool(site: str, area_km2: float, cache: Path):
    """Pooled three-seed samples [issue, lead, 132] in ft3/s, with their issue times and leads."""
    paths = [download(f"{p}/run/hindcast_mixture/site_id=USGS-{site}/lstm_full_v2_snodas.parquet", cache / f"flowmix_{site}_{s}.parquet") for s, p in FLOW_RUNS.items()]
    frames = [pd.read_parquet(p) for p in paths]
    keys = ["issue_time", "lead_h", "member"]
    common = frames[0][keys]
    for f in frames[1:]:
        common = common.merge(f[keys], on=keys)
    common = common.sort_values(keys).reset_index(drop=True)
    issues = pd.DatetimeIndex(sorted(common["issue_time"].unique()))
    leads = np.sort(common["lead_h"].unique())
    members = np.sort(common["member"].unique())
    assert len(common) == len(issues) * len(leads) * len(members)
    check_validation(issues, f"{site} flow issues")
    rng = np.random.default_rng([0, zlib.crc32(f"USGS-{site}".encode())])
    draws = []
    for f in frames:
        assert str(f["unit"].iloc[0]) == "mm/h"
        aligned = common.merge(f, on=keys, how="left")
        p = tuple(aligned[[f"{name}{c}" for c in range(3)]].to_numpy(np.float64) for name in ("pi", "mu", "b", "tau"))
        draws.append(sample_cmal(*p, DRAWS_PER_MEMBER, rng).reshape(len(issues), len(leads), len(members) * DRAWS_PER_MEMBER))
    vals = np.clip(np.concatenate(draws, axis=2), 0, None) / 3.6 * area_km2 * M3S_TO_CFS
    return issues, leads, vals


class FlowCalibration:
    """Applies PR #51's `flow-calibrate` output (`FlowCalibration.load` + `apply_long` in `calibrate.py`)."""

    def __init__(self, path: Path, basin: str):
        meta = json.loads((path / "flow_calibration.json").read_text())
        self.levels = np.asarray(meta["levels"], float)
        self.flash_edges = tuple(meta["flash_edges"])
        self.pct_edges = tuple(meta["pct_edges"])
        self.params = pd.read_csv(path / "flow_calibration.csv")
        if (path / "flow_boost.csv").exists():
            raise RuntimeError("flow_boost.csv present: the tail boost is deferred and must stay off here")
        st = pd.read_parquet(path / "basin_stats.parquet").loc[basin]
        self.delta = float(st["delta"])
        self.rb = float(st["rb"])
        self.quantiles = np.asarray(st[[f"q{lv:.3f}" for lv in self.levels]], float)
        self.years = sorted(int(w) for w in self.params["wy"].unique() if w != 0)

    def percentile(self, values: np.ndarray) -> np.ndarray:
        q, keep = np.unique(self.quantiles, return_index=True)
        return np.interp(values, q, self.levels[keep])

    def lookup(self, wy: int, lead_h: float, pct: np.ndarray) -> np.ndarray:
        p = self.params[(self.params["wy"] == wy) & (self.params["lead_h"] == lead_h)].set_index("cell")
        cols = ["shift", "s_lo", "s_hi"]
        out = np.tile(p.loc[-1, cols].to_numpy(float), (len(pct), 1))
        n_flash = len(self.flash_edges) + 1
        pbin = np.digitize(pct, self.pct_edges)
        cell = pbin * n_flash + int(np.digitize(self.rb, self.flash_edges))
        for c, row in p.drop(index=-1).iterrows():
            if c >= 100:
                out[pbin == c - 100] = row[cols].to_numpy(float)
        for c, row in p.drop(index=-1).iterrows():
            if c < 100:
                out[cell == c] = row[cols].to_numpy(float)
        return out

    def apply(self, issues: pd.DatetimeIndex, leads: np.ndarray, vals: np.ndarray) -> np.ndarray:
        out = np.empty_like(vals)
        wy = water_year(issues)
        fold = np.where(np.isin(wy, self.years), wy, 0)
        for j, lead in enumerate(leads):
            x = vals[:, j, :]
            z = np.log(x + self.delta)
            m = np.median(z, axis=1)
            pct = self.percentile(np.median(x, axis=1))
            for fw in np.unique(fold):
                rows = fold == fw
                p = self.lookup(int(fw), float(lead), pct[rows])
                d = z[rows] - m[rows, None]
                s = np.where(d > 0, p[:, 2:3], p[:, 1:2])
                out[rows, j, :] = np.maximum(np.exp(m[rows, None] + p[:, 0:1] + s * d) - self.delta, 0.0)
        return out


def block_bootstrap_ratio(num: np.ndarray, den: np.ndarray, days: np.ndarray, rng, draws: int = BOOTSTRAP_DRAWS) -> tuple[float, float]:
    """95% interval of 1 - sum(num)/sum(den), resampling 7-day blocks of issue dates."""
    blocks = (days - days.min()) // 7
    ub, inv = np.unique(blocks, return_inverse=True)
    n_sum = np.bincount(inv, num, len(ub))
    d_sum = np.bincount(inv, den, len(ub))
    pick = rng.integers(0, len(ub), (draws, len(ub)))
    sk = 1 - n_sum[pick].sum(1) / d_sum[pick].sum(1)
    return float(np.percentile(sk, 2.5)), float(np.percentile(sk, 97.5))


def flow_skill(issues, leads, cal_vals, raw_vals, obs: pd.Series) -> dict:
    cycle = (issues.hour % 6 == 0) & (issues.minute == 0)
    it = issues[cycle]
    pers, _ = last_available(obs, it, 1.0, 6.0)
    rng = np.random.default_rng(7)
    days = seconds(it) // 86400
    rows = []
    for j, lead in enumerate(leads):
        y = lookup_obs(obs, it + pd.Timedelta(hours=float(lead)))
        ok = np.isfinite(y) & np.isfinite(pers)
        c_cal = crps_ensemble(cal_vals[cycle][ok, j, :], y[ok])
        c_raw = crps_ensemble(raw_vals[cycle][ok, j, :], y[ok])
        c_p = np.abs(pers[ok] - y[ok])
        q = np.quantile(cal_vals[cycle][ok, j, :], [0.05, 0.25, 0.5, 0.75, 0.95], axis=1)
        lo, hi = block_bootstrap_ratio(c_cal, c_p, days[ok], rng)
        rows.append({
            "lead_h": float(lead), "n": int(ok.sum()),
            "skill": 1 - c_cal.sum() / c_p.sum(), "skill_lo": lo, "skill_hi": hi,
            "skill_raw": 1 - c_raw.sum() / c_p.sum(),
            "crps": c_cal.mean(), "crps_persistence": c_p.mean(),
            "mae_median": np.abs(q[2] - y[ok]).mean(),
            "cover90": float(((y[ok] >= q[0]) & (y[ok] <= q[4])).mean()),
            "cover50": float(((y[ok] >= q[1]) & (y[ok] <= q[3])).mean()),
        })
    return {"rows": rows, "issues": int(cycle.sum())}


def marfc_bulletins(site: str, cache: Path) -> pd.DataFrame:
    local = cache / f"marfc_{site}.parquet"
    if local.exists():
        return pd.read_parquet(local)
    arch = next(b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith("flowcast-archiver-"))
    df = read_archive(Lake(f"s3://{arch}/baselines"), ["marfc_rvf"], site, since=VALIDATION_START)
    df = df[(df["variable"] == "discharge") & (df["issue_time"] <= VALIDATION_END)]
    df = df[["issue_time", "valid_time", "lead_h", "value"]].reset_index(drop=True)
    df.to_parquet(local, index=False)
    return df


def marfc_skill(issues, leads, cal_vals, obs: pd.Series, marfc: pd.DataFrame) -> dict:
    """Model CRPS vs MARFC absolute error at MARFC's own issue times, binned onto the harness leads."""
    m = marfc[marfc["issue_time"].isin(issues)].copy()
    m["lead_bin"] = lead_bin(m["lead_h"], tuple(float(x) for x in leads)).to_numpy()
    m = m.dropna(subset=["lead_bin", "value"]).sort_values("lead_h").drop_duplicates(["issue_time", "lead_bin"], keep="last")
    m["y"] = lookup_obs(obs, m["valid_time"].to_numpy())
    lead_idx = {float(l): j for j, l in enumerate(leads)}
    rng = np.random.default_rng(11)
    rows = []
    for lb in MARFC_LEADS:
        g = m[(m["lead_bin"] == lb) & np.isfinite(m["y"])]
        if g.empty:
            continue
        j = lead_idx[lb]
        p = issues.get_indexer(pd.DatetimeIndex(g["issue_time"]))
        y_model = lookup_obs(obs, pd.DatetimeIndex(g["issue_time"]) + pd.Timedelta(hours=lb))
        ok = np.isfinite(y_model)
        c_m = crps_ensemble(cal_vals[p[ok], j, :], y_model[ok])
        e_n = np.abs(g["value"].to_numpy(float)[ok] - g["y"].to_numpy(float)[ok])
        days = seconds(pd.DatetimeIndex(g["issue_time"])[ok]) // 86400
        lo, hi = block_bootstrap_ratio(c_m, e_n, days, rng)
        rows.append({"lead_h": lb, "n": int(ok.sum()), "skill": 1 - c_m.sum() / e_n.sum(), "skill_lo": lo, "skill_hi": hi, "crps_model": c_m.mean(), "mae_marfc": e_n.mean()})
    return {"rows": rows, "issues": int(m["issue_time"].nunique())}


# ---------------------------------------------------------------------------------------------- temperature


def temp_frames(site: str, cache: Path) -> pd.DataFrame:
    frames = []
    for r, (s, p) in enumerate(TEMP_RUNS.items()):
        d = pd.read_parquet(download(f"{p}/run/hindcast_coh/site_id=USGS-{site}/lstm_temp_v1.parquet", cache / f"temp_{site}_{s}.parquet"),
                            columns=["variable", "issue_time", "valid_time", "lead_h", "member", "value"])
        frames.append(d.assign(member=d["member"] + 1000 * r))
    d = pd.concat(frames, ignore_index=True)
    check_validation(d["issue_time"], f"{site} temperature issues")
    return d


def calibrate_temp(v: np.ndarray, offset: np.ndarray, scale: np.ndarray) -> np.ndarray:
    med = np.nanmedian(v, axis=1, keepdims=True)
    return med + offset[:, None] + scale[:, None] * (v - med)


def temp_hourly(d: pd.DataFrame, cal: pd.DataFrame):
    h = d[(d["variable"] == "water_temperature") & (d["lead_h"] > 0)]
    wide = h.pivot_table(index=["issue_time", "lead_h"], columns="member", values="value")
    idx = wide.index.to_frame(index=False)
    wy = water_year(idx["issue_time"])
    c = cal[cal["variable"] == "water_temperature"]
    years = sorted(int(w) for w in c["wy"].unique() if w != 0)
    key = pd.DataFrame({"lead_h": idx["lead_h"], "wy": np.where(np.isin(wy, years), wy, 0)}).merge(c[["lead_h", "wy", "a", "scale"]], on=["lead_h", "wy"], how="left")
    v = calibrate_temp(wide.to_numpy(float), key["a"].to_numpy(float), key["scale"].to_numpy(float))
    issues = pd.DatetimeIndex(sorted(idx["issue_time"].unique()))
    leads = np.sort(idx["lead_h"].unique())
    full = pd.MultiIndex.from_product([issues, leads], names=["issue_time", "lead_h"])
    arr = pd.DataFrame(v, index=wide.index).reindex(full).to_numpy().reshape(len(issues), len(leads), -1)
    return issues, leads, arr


def gefs_basin(cube: xr.Dataset, basin: str) -> dict:
    inits = pd.DatetimeIndex(cube.gefs_init.values).tz_localize("UTC")
    check_validation(inits, "GEFS inits")
    sel = cube[["gefs_precip_mm_h", "gefs_temp_2m_c"]].sel(basin=basin).load()
    return {"inits": inits, "leads": cube.gefs_lead.values.astype(float), "precip": sel.gefs_precip_mm_h.values, "temp": sel.gefs_temp_2m_c.values}


def gefs_init_for(g: dict, issues: pd.DatetimeIndex) -> np.ndarray:
    return g["inits"].searchsorted(issues - pd.Timedelta(hours=GEFS_LATENCY_H), side="right") - 1


def gefs_daily_max_air(g: dict, issues: pd.DatetimeIndex, n_days: int) -> np.ndarray:
    """`tempscore.gefs_daily_max_air` (PR #51): member-mean (members 0-4) daily-high air temperature per lead day."""
    mean = np.nanmean(g["temp"][:, :GEFS_TEMP_MEMBERS, :], axis=1)
    out = np.full((len(issues), n_days), np.nan)
    pos = gefs_init_for(g, issues)
    issue_date = local_days(issues + pd.Timedelta(hours=1))
    for i, p in enumerate(pos):
        if p < 0:
            continue
        valid = g["inits"][p] + pd.to_timedelta(g["leads"], unit="h")
        keep = valid > issues[i]
        dates = local_days(valid[keep])
        day = (dates - issue_date[i]).astype(int)
        v = mean[p][keep]
        for k in range(n_days):
            s = (day == k) & np.isfinite(v)
            if s.sum() >= 3:
                out[i, k] = v[s].max()
    return out


def temp_daily(d: pd.DataFrame, cal: pd.DataFrame, g: dict, below_dam: bool):
    x = d[(d["variable"] == "water_temperature_daily_max") & (d["issue_time"].dt.hour == 12) & (d["issue_time"].dt.minute == 0)]
    wide = x.pivot_table(index=["issue_time", "lead_h", "valid_time"], columns="member", values="value")
    idx = wide.index.to_frame(index=False)
    issues = pd.DatetimeIndex(sorted(idx["issue_time"].unique()))
    n_days = int(idx["lead_h"].max() // 24) + 1
    ta = gefs_daily_max_air(g, issues, n_days)
    dt = (ta - ta[:, :1])
    i_pos = issues.get_indexer(pd.DatetimeIndex(idx["issue_time"]))
    k = (idx["lead_h"].to_numpy() // 24).astype(int)
    dts = np.clip(np.nan_to_num(dt[i_pos, k]), -WARMUP_CLIP, WARMUP_CLIP)
    warm = pd.DatetimeIndex(idx["valid_time"]).month.isin(WARM_MONTHS).astype(int)
    wy = water_year(idx["issue_time"])
    c = cal[cal["variable"] == "water_temperature_daily_max"]
    years = sorted(int(w) for w in c["wy"].unique() if w != 0)
    key = pd.DataFrame({"lead_h": idx["lead_h"], "group": 2 * int(below_dam) + warm, "wy": np.where(np.isin(wy, years), wy, 0)})
    key = key.merge(c[["lead_h", "group", "wy", "a", "b_up", "b_down", "scale"]], on=["lead_h", "group", "wy"], how="left")
    offset = key["a"].to_numpy() + key["b_up"].to_numpy() * np.maximum(dts, 0) + key["b_down"].to_numpy() * np.minimum(dts, 0)
    v = calibrate_temp(wide.to_numpy(float), offset, key["scale"].to_numpy(float))
    return idx, v


def temp_skill(issues, leads, arr, tw: pd.Series) -> dict:
    """Hourly CRPS skill vs diurnal persistence (same hour of the latest observed day), cycle issues."""
    cycle = (issues.hour % 6 == 0) & (issues.minute == 0)
    it = issues[cycle]
    s = tw.dropna()
    rows = []
    for j, lead in enumerate(leads):
        y = lookup_obs(s, it + pd.Timedelta(hours=float(lead)))
        k = np.ceil((lead + 1.0) / 24.0)
        p = lookup_obs(s, it + pd.Timedelta(hours=float(lead - 24 * k)))
        ens = arr[cycle][:, j, :]
        ok = np.isfinite(y) & np.isfinite(p) & np.isfinite(ens).all(axis=1)
        if ok.sum() < 50:
            continue
        c = crps_ensemble(ens[ok], y[ok])
        e = np.abs(p[ok] - y[ok])
        q = np.quantile(ens[ok], [0.05, 0.95], axis=1)
        rows.append({"lead_h": float(lead), "n": int(ok.sum()), "skill": 1 - c.sum() / e.sum(), "crps": c.mean(), "mae_persistence": e.mean(),
                     "mae_median": np.abs(np.median(ens[ok], axis=1) - y[ok]).mean(), "cover90": float(((y[ok] >= q[0]) & (y[ok] <= q[1])).mean())})
    return {"rows": rows}


def temp_daily_skill(idx: pd.DataFrame, v: np.ndarray, tmax: pd.Series) -> dict:
    """Daily-high CRPS skill vs yesterday's observed high, 12Z issues."""
    vt = pd.DatetimeIndex(idx["valid_time"])
    y = tmax.reindex(vt).to_numpy(float)
    issue_day = pd.DatetimeIndex(local_days(pd.DatetimeIndex(idx["issue_time"]) + pd.Timedelta(hours=1))).tz_localize("UTC")
    p = tmax.reindex(issue_day - pd.Timedelta(days=1)).to_numpy(float)
    rows = []
    for lead in sorted(idx["lead_h"].unique()):
        sel = (idx["lead_h"] == lead).to_numpy() & np.isfinite(y) & np.isfinite(p) & np.isfinite(v).all(axis=1)
        if sel.sum() < 30:
            continue
        c = crps_ensemble(v[sel], y[sel])
        e = np.abs(p[sel] - y[sel])
        rows.append({"lead_day": int(lead // 24), "n": int(sel.sum()), "skill": 1 - c.sum() / e.sum(), "crps": c.mean(), "mae_persistence": e.mean(),
                     "mae_median": np.abs(np.median(v[sel], axis=1) - y[sel]).mean()})
    return {"rows": rows}


# ---------------------------------------------------------------------------------------------- precipitation forcing


def gefs_precip_bins(g: dict, issues: pd.DatetimeIndex, bin_h: int = 6, horizon_h: int = 168) -> dict:
    """Basin-mean GEFS precipitation behind each issue's forecast: per 6 h bin, the 11-member mean, 10th and 90th
    percentile (mm), and the share of member precipitation falling at or below 0.5 degC air temperature."""
    n_bins = horizon_h // bin_h
    out = np.full((len(issues), n_bins, 4), np.nan)
    totals = np.full((len(issues), 3), np.nan)
    pos = gefs_init_for(g, issues)
    step = np.diff(g["leads"]).min()
    for i, p in enumerate(pos):
        if p < 0:
            continue
        valid = g["inits"][p] + pd.to_timedelta(g["leads"], unit="h")
        hours = ((valid - issues[i]) / pd.Timedelta(hours=1)).to_numpy()
        keep = (hours > 0) & (hours <= horizon_h)
        pr = g["precip"][p][:, keep] * step
        tt = g["temp"][p][:, keep]
        b = ((hours[keep] - 1e-9) // bin_h).astype(int)
        for k in range(n_bins):
            s = b == k
            if not s.any():
                continue
            mem = np.nansum(pr[:, s], axis=1)
            snow = np.nansum(np.where(tt[:, s] <= 0.5, pr[:, s], 0), axis=1)
            out[i, k] = [mem.mean(), np.percentile(mem, 10), np.percentile(mem, 90), snow.sum() / mem.sum() if mem.sum() > 0.05 else 0]
        tot = np.nansum(pr, axis=1)
        totals[i] = [tot.mean(), np.percentile(tot, 10), np.percentile(tot, 90)]
    return {"bin_h": bin_h, "bins": out, "totals": totals}


# ---------------------------------------------------------------------------------------------- climatology


def doy_climatology(daily: pd.Series, window: int = 7) -> np.ndarray:
    """[366, 5] 10/25/50/75/90th percentiles of daily means within +-window days of each day of year."""
    d = daily.dropna()
    doy = d.index.dayofyear.to_numpy()
    vals = d.to_numpy()
    out = np.full((366, 5), np.nan)
    for k in range(1, 367):
        dist = np.minimum(np.abs(doy - k), 366 - np.abs(doy - k))
        v = vals[dist <= window]
        if len(v) >= 30:
            out[k - 1] = np.percentile(v, [10, 25, 50, 75, 90])
    return out


def doy_percentile(daily: pd.Series, value: float, doy: int, window: int = 7) -> float | None:
    d = daily.dropna()
    if not np.isfinite(value) or d.empty:
        return None
    dd = d.index.dayofyear.to_numpy()
    dist = np.minimum(np.abs(dd - doy), 366 - np.abs(dd - doy))
    v = d.to_numpy()[dist <= window]
    if len(v) < 30:
        return None
    return float((v < value).mean() + 0.5 * (v == value).mean())


def local_daily_mean(hourly: pd.Series) -> pd.Series:
    s = hourly.dropna()
    out = s.groupby(local_days(s.index)).mean()
    out.index = pd.DatetimeIndex(out.index)
    return out


# ---------------------------------------------------------------------------------------------- events


def pick_events(q: pd.Series, tmax: pd.Series, swe: pd.Series, n_floods: int = 3) -> list[dict]:
    """Largest separated flow peaks of WY2021-2022, the hottest day, and the main snowmelt if there is one."""
    s = q.dropna()
    events = []
    taken = []
    for t, v in s.sort_values(ascending=False).items():
        if all(abs((t - u) / pd.Timedelta(days=1)) > 10 for u in taken):
            taken.append(t)
            events.append({"kind": "flood", "time": t, "value": float(v)})
        if len(taken) >= n_floods:
            break
    if not tmax.dropna().empty:
        t = tmax.idxmax()
        events.append({"kind": "heat", "time": t + pd.Timedelta(hours=19), "value": float(tmax.max())})
    w = swe.dropna()
    if not w.empty and w.max() > 50:
        drop = (w.shift(0) - w.shift(-7 * 24)).dropna()
        t = drop.idxmax()
        later = s[t: t + pd.Timedelta(days=21)]
        if len(later):
            events.append({"kind": "melt", "time": later.idxmax(), "value": float(later.max()), "swe_peak_mm": float(w.max())})
    return events


def event_issue(issues: pd.DatetimeIndex, peak: pd.Timestamp, lead_h: float = 48.0) -> int:
    """The 00/12Z issue closest to `lead_h` before the peak."""
    cands = np.flatnonzero((issues.hour % 12 == 0) & (issues.minute == 0) & (issues <= peak - pd.Timedelta(hours=12)))
    if not len(cands):
        return 0
    target = peak - pd.Timedelta(hours=lead_h)
    return int(cands[np.argmin(np.abs(seconds(issues[cands]) - seconds([target])[0]))])


# ---------------------------------------------------------------------------------------------- geometry


def fetch_geo(site: str, cfg: dict, cube: xr.Dataset, usgs: WaterDataClient) -> dict:
    info = get_json(f"{NLDI}/nwissite/USGS-{site}")
    comid = int(info["features"][0]["properties"]["comid"])
    gauge_xy = info["features"][0]["geometry"]["coordinates"]
    basin = get_json(f"{NLDI}/nwissite/USGS-{site}/basin", simplified="true")
    poly = shape(basin["features"][0]["geometry"])
    x0, y0, x1, y1 = poly.bounds
    ut = get_json(f"{NLDI}/comid/{comid}/navigation/UT/flowlines", distance=2000)
    upstream = {int(f["properties"]["nhdplus_comid"]) for f in ut["features"]}
    props = "comid,gnis_name,streamorde,totdasqkm,qe_ma,the_geom"
    cql = f"streamorde>={cfg['stream_order_min']} AND BBOX(the_geom,{x0 - 0.01},{y0 - 0.01},{x1 + 0.01},{y1 + 0.01})"
    wfs = get_json(WFS, service="WFS", version="1.0.0", request="GetFeature", typeName="wmadata:nhdflowline_network", outputFormat="application/json", propertyName=props, CQL_FILTER=cql)
    rivers = []
    for f in wfs["features"]:
        p = f["properties"]
        if p["comid"] not in upstream:
            continue
        lines = f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiLineString" else [f["geometry"]["coordinates"]]
        rivers.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": round_coords([c[:2] for c in sum(lines, [])])},
                       "properties": {"name": (p.get("gnis_name") or "").strip() or None, "order": p["streamorde"], "area": round(p["totdasqkm"] or 0, 1), "qma": round(p.get("qe_ma") or 0, 1)}})

    nwis = get_json(f"{NLDI}/nwissite/USGS-{site}/navigation/UT/nwissite", distance=2000)
    ids = [f["properties"]["identifier"].removeprefix("USGS-") for f in nwis["features"]]
    ids = [i for i in ids if i != site]
    latest = pd.concat([usgs.latest_continuous(ids[k:k + 50], "00060") for k in range(0, len(ids), 50)], ignore_index=True) if ids else pd.DataFrame(columns=["monitoring_location_id", "time", "value"])
    latest = latest.set_index(latest["monitoring_location_id"].str.removeprefix("USGS-"))
    now = pd.Timestamp.now(tz="UTC")
    model_inputs = set(filter(None, str(cube.upstream_sum_sites.sel(basin=site).values).split(","))) | set(filter(None, str(cube.gauged_outflow_sites.sel(basin=site).values).split(",")))
    below_dam = set(filter(None, str(cube.gauged_outflow_sites.sel(basin=site).values).split(",")))
    gauges = []
    for f in nwis["features"]:
        sid = f["properties"]["identifier"].removeprefix("USGS-")
        if sid == site:
            continue
        active = sid in latest.index and (now - latest.loc[sid, "time"]) < pd.Timedelta(days=14)
        if not active and sid not in model_inputs:
            continue
        gauges.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": round_coords(f["geometry"]["coordinates"])},
                       "properties": {"id": sid, "name": f["properties"]["name"].title().replace(" Ny", ", NY").replace(" Me", ", ME").replace(" Va", ", VA"),
                                      "q": float(latest.loc[sid, "value"]) if active else None, "active": bool(active),
                                      "role": "below_dam" if sid in below_dam else ("model_input" if sid in model_inputs else "upstream")}})

    dams = []
    offset = 0
    while True:
        page = get_json(NID, where="1=1", geometry=f"{x0},{y0},{x1},{y1}", geometryType="esriGeometryEnvelope", inSR=4326, outSR=4326, f="geojson",
                        outFields="NAME,NIDID,NID_STORAGE,NORMAL_STORAGE,PRIMARY_PURPOSE,YEAR_COMPLETED,NID_HEIGHT,RIVER_OR_STREAM",
                        resultOffset=offset, resultRecordCount=1000)
        feats = page.get("features", [])
        for f in feats:
            xy = f["geometry"]["coordinates"]
            if not poly.contains(Point(xy)):
                continue
            p = f["properties"]
            storage = p.get("NID_STORAGE") or p.get("NORMAL_STORAGE") or 0
            dams.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": round_coords(xy)},
                         "properties": {"name": (p.get("NAME") or "").title(), "id": p.get("NIDID"), "storage_af": float(storage), "purpose": p.get("PRIMARY_PURPOSE"),
                                        "year": p.get("YEAR_COMPLETED"), "height_ft": p.get("NID_HEIGHT"), "river": (p.get("RIVER_OR_STREAM") or "").title()}})
        if len(feats) < 1000:
            break
        offset += 1000
    dams.sort(key=lambda f: -f["properties"]["storage_af"])

    geom = basin["features"][0]["geometry"]
    return {
        "basin": {"type": "Feature", "geometry": {"type": geom["type"], "coordinates": round_coords(geom["coordinates"], 4)}, "properties": {}},
        "gauge": {"type": "Feature", "geometry": {"type": "Point", "coordinates": round_coords(gauge_xy)}, "properties": {"id": site, "name": cfg["name"]}},
        "rivers": {"type": "FeatureCollection", "features": rivers},
        "gauges": {"type": "FeatureCollection", "features": gauges},
        "dams": {"type": "FeatureCollection", "features": dams},
        "bounds": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
        "sources": {"basin": "USGS NLDI (NHDPlus V2 catchments)", "rivers": "NHDPlus V2 flowlines upstream of the gauge (USGS NLDI + wmadata GeoServer)",
                    "gauges": "USGS NLDI upstream NWIS sites; active = discharge reported in the last 14 days (USGS Water Data API)", "dams": "USACE National Inventory of Dams"},
    }


# ---------------------------------------------------------------------------------------------- recent observations


def hourly_mean(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    s = df.set_index("time")["value"].astype(float)
    return s.resample("1h", label="right", closed="right").mean().dropna()


def fetch_recent(site: str, usgs: WaterDataClient, days: int = 45) -> dict:
    end = pd.Timestamp.now(tz="UTC").floor("h")
    start = end - pd.Timedelta(days=days)
    out = {"fetched_at": end.isoformat()}
    for code, name in (("00060", "flow"), ("00010", "temp"), ("00065", "stage")):
        try:
            s = hourly_mean(usgs.continuous(site, code, start, end, use_cache=False))
        except Exception as exc:
            print(f"  {site} {code}: {type(exc).__name__}", flush=True)
            s = pd.Series(dtype=float)
        out[name] = {"t": epoch(s.index), "v": jsonable(s.to_numpy(), digits=4 if name != "temp" else None, decimals=2 if name == "temp" else None)}
    year = usgs.daily(site, "00060", (end - pd.Timedelta(days=400)).date(), end.date(), use_cache=False)
    out["daily_flow"] = {"d": [str(d.date()) for d in pd.DatetimeIndex(year["date"])], "v": jsonable(year["value"].astype(float).to_numpy(), 4)} if not year.empty else {"d": [], "v": []}
    return out


# ---------------------------------------------------------------------------------------------- site build


def build_site(site: str, cfg: dict, cube: xr.Dataset, sel: pd.DataFrame, cal_dir: Path, cache: Path, out: Path, usgs: WaterDataClient, skip_geo: bool) -> dict:
    print(f"{site} {cfg['short']}", flush=True)
    area = float(cube.area_km2.sel(basin=site).values)
    below_dam = bool(cube.below_dam.sel(basin=site).values > 0)
    q_all = series(cube, "qobs_m3s", site) * M3S_TO_CFS
    tw_all = series(cube, "tw_c", site)
    swe = series(cube, "snodas_swe_mm", site, VALIDATION_START)
    q_val, tw_val = q_all[VALIDATION_START:], tw_all[VALIDATION_START:]

    issues, leads, raw = flow_pool(site, area, cache)
    cal = FlowCalibration(cal_dir / "flow", site)
    vals = cal.apply(issues, leads, raw)
    skill = {"flow": flow_skill(issues, leads, vals, raw, q_val), "national_flow": NATIONAL_FLOW_SKILL}
    marfc = None
    if cfg["marfc"]:
        marfc = marfc_bulletins(site, cache)
        check_validation(marfc["issue_time"], "MARFC bulletins")
        skill["marfc"] = marfc_skill(issues, leads, vals, q_val, marfc)

    tcal = pd.read_csv(cal_dir / "temperature/calibration.csv")
    td = temp_frames(site, cache)
    t_issues, t_leads, t_arr = temp_hourly(td, tcal)
    g = gefs_basin(cube, site)
    d_idx, d_vals = temp_daily(td, tcal, g, below_dam)
    tmax = daily_max(tw_val)
    skill["temp"] = temp_skill(t_issues, t_leads, t_arr, tw_val)
    skill["temp_daily"] = temp_daily_skill(d_idx, d_vals, tmax)

    show = (issues.minute == 0) & (issues.hour % 12 == 0)
    if cfg["marfc"]:
        show |= issues.isin(pd.DatetimeIndex(marfc["issue_time"].unique()))
    sh = issues[show]
    fq = np.quantile(vals[show], QUANTS, axis=2).transpose(1, 2, 0)
    t_pos = t_issues.get_indexer(sh)
    t_lead_pos = t_leads.searchsorted(leads)
    tq = np.full((len(sh), len(leads), len(QUANTS)), np.nan)
    have = t_pos >= 0
    if have.any():
        tq[have] = np.nanquantile(t_arr[t_pos[have]][:, t_lead_pos, :], QUANTS, axis=2).transpose(1, 2, 0)
    d_issues = pd.DatetimeIndex(sorted(d_idx["issue_time"].unique()))
    dq = np.full((len(sh), 8, len(QUANTS)), np.nan)
    dpos = d_issues.get_indexer(sh)
    dqs = np.quantile(d_vals, QUANTS, axis=1).T
    di = d_issues.get_indexer(pd.DatetimeIndex(d_idx["issue_time"]))
    dk = (d_idx["lead_h"].to_numpy() // 24).astype(int)
    lookup = {(a, b): r for r, (a, b) in enumerate(zip(di, dk))}
    for i, p in enumerate(dpos):
        if p < 0:
            continue
        for k in range(8):
            r = lookup.get((p, k))
            if r is not None:
                dq[i, k] = dqs[r]
    pr = gefs_precip_bins(g, sh)

    marfc_traces = {}
    if marfc is not None:
        for t, grp in marfc[marfc["issue_time"].isin(sh)].groupby("issue_time"):
            grp = grp.sort_values("valid_time").drop_duplicates("valid_time", keep="last")
            marfc_traces[str(int(sh.get_loc(t)))] = {"t": epoch(grp["valid_time"]), "v": jsonable(grp["value"].to_numpy(), 3)}

    events = pick_events(q_val, tmax, swe)
    for e in events:
        e["issue"] = event_issue(sh, e["time"], 60.0 if e["kind"] == "melt" else 48.0 if e["kind"] == "flood" else 72.0)
        e["time"] = int(e["time"].timestamp())

    sd = out / "sites" / site
    write(sd / "hindcast.json", {
        "issues": epoch(sh), "leads": [float(x) for x in leads], "quants": QUANTS.tolist(),
        "flow": jsonable(fq, 3), "temp": jsonable(tq, None, 1),
        "tmax": jsonable(dq, None, 1), "marfc": marfc_traces,
        "precip": {"bin_h": pr["bin_h"], "fields": ["mean", "p90", "snow_share"], "bins": jsonable(pr["bins"][:, :, [0, 2, 3]], None, 1),
                   "totals": jsonable(pr["totals"], None, 1)},
        "is_marfc_time": [bool(x) for x in (~((sh.minute == 0) & (sh.hour % 12 == 0)))],
        "events": events,
        "model": {"flow": "final 3-seed SNODAS LSTM (seeds 42, 43, 44), 11 GEFS members x 4 draws per seed = 132 samples, PR #51 conditional stretch (boost off)",
                  "temp": "water temperature v1, seeds 42 + 44, 40 coherent members, PR #51 calibration (hourly offset + spread; daily-high warm-up offset)"},
    })
    write(sd / "observed.json", {
        "flow": {"t0": int(q_val.index[0].timestamp()), "v": jsonable(q_val.to_numpy(), 4)},
        "temp": {"t0": int(tw_val.index[0].timestamp()), "v": jsonable(tw_val.to_numpy(), None, 2)},
        "tmax": {"d": epoch(tmax.index), "v": jsonable(tmax.to_numpy(), None, 2)},
        "swe": {"t0": int(swe.index[0].timestamp()), "step_h": 24, "v": jsonable(swe.iloc[::24].to_numpy(), None, 0)},
    })
    write(sd / "skill.json", json.loads(pd.Series(skill).to_json()))

    q_daily = local_daily_mean(q_all)
    t_daily = local_daily_mean(tw_all)
    clim = {"flow": jsonable(doy_climatology(q_daily), 3), "temp": jsonable(doy_climatology(t_daily), None, 1), "years": f"{q_daily.index.min():%Y}-{q_daily.index.max():%Y}"}
    write(sd / "climatology.json", clim)

    recent = fetch_recent(site, usgs)
    write(sd / "recent.json", recent)
    if not skip_geo:
        write(sd / "geo.json", fetch_geo(site, cfg, cube, usgs))

    st = sel.loc[site]
    meta = {
        "id": site, **cfg, "area_km2": area, "area_mi2": area / 2.58999,
        "lat": float(cube.lat.sel(basin=site).values), "lon": float(cube.lon.sel(basin=site).values), "state": st["STATE"],
        "elevation_m": float(cube.elevation_m.sel(basin=site).values), "slope_deg": float(cube.slope_deg.sel(basin=site).values),
        "forest_frac": float(cube.forest_frac.sel(basin=site).values), "developed_frac": float(cube.developed_frac.sel(basin=site).values),
        "snow_frac": float(cube.frac_snow.sel(basin=site).values), "baseflow_index": float(cube.baseflow_index.sel(basin=site).values),
        "precip_mm_yr": float(cube.p_mean.sel(basin=site).values) * 365.25, "air_temp_c": float(cube.t_mean.sel(basin=site).values),
        "runoff_mm_yr": float(cube.runoff_mean_mm_yr.sel(basin=site).values), "flashiness_rb": cal.rb,
        "travel_time_mean_h": float(cube.tt_mean_h.sel(basin=site).values), "travel_time_max_h": float(cube.tt_max_h.sel(basin=site).values),
        "nid_dams": int(cube.nid_n_dams.sel(basin=site).values), "nid_major_dams": int(cube.nid_n_major.sel(basin=site).values), "below_dam": below_dam,
        "mean_flow_cfs": float(q_all[:"2019-09-30"].mean()), "median_flow_cfs": float(q_all[:"2019-09-30"].median()),
        "q99_cfs": float(q_all[:"2019-09-30"].quantile(0.99)), "record_validation_cfs": float(q_val.max()),
        "gages_class": st["CLASS"],
    }
    write(sd / "meta.json", json.loads(pd.Series(meta).to_json()))
    f = skill["flow"]["rows"]
    print("  flow skill vs persistence (cal / raw):", " ".join(f"{r['lead_h']:.0f}h {r['skill']:+.2f}/{r['skill_raw']:+.2f}" for r in f if r["lead_h"] in (1, 6, 12, 24, 48, 72, 120, 168)), flush=True)
    if "marfc" in skill:
        print("  vs MARFC:", " ".join(f"{r['lead_h']:.0f}h {r['skill']:+.2f} [{r['skill_lo']:+.2f},{r['skill_hi']:+.2f}]" for r in skill["marfc"]["rows"]), flush=True)
    return meta


# ---------------------------------------------------------------------------------------------- overview


def build_overview(cube: xr.Dataset, sel: pd.DataFrame, cal_dir: Path, out: Path, usgs: WaterDataClient, cache: Path) -> None:
    print("overview: all model basins", flush=True)
    basins = [str(b) for b in cube.basin.values]
    clim_path = cache / "daily_flow_all.parquet"
    if clim_path.exists():
        daily = pd.read_parquet(clim_path)
    else:
        da = cube.qobs_m3s.sel(time=slice(CLIMATOLOGY_START.tz_convert(None), VALIDATION_END.tz_convert(None)))
        da = (da * M3S_TO_CFS).resample(time="1D", offset="5h").mean().load()
        daily = pd.DataFrame(da.values.T, index=pd.DatetimeIndex(da.time.values), columns=basins)
        daily.to_parquet(clim_path)
    latest = pd.concat([usgs.latest_continuous(basins[k:k + 50], "00060") for k in range(0, len(basins), 50)], ignore_index=True)
    latest = latest.drop_duplicates("monitoring_location_id", keep="last").set_index(latest["monitoring_location_id"].drop_duplicates(keep="last").str.removeprefix("USGS-"))
    stats = pd.read_parquet(cal_dir / "flow/basin_stats.parquet")
    now = pd.Timestamp.now(tz="UTC")
    doy = now.tz_convert(LOCAL_TZ).dayofyear
    rows = []
    for b in basins:
        st = sel.loc[b] if b in sel.index else None
        q = t = None
        pct = None
        if b in latest.index and (now - latest.loc[b, "time"]) < pd.Timedelta(days=3) and np.isfinite(latest.loc[b, "value"]):
            q = float(latest.loc[b, "value"])
            t = int(latest.loc[b, "time"].timestamp())
            pct = doy_percentile(daily[b], q, doy)
        rb = float(stats.loc[b, "rb"]) if b in stats.index else float("nan")
        snow = float(cube.frac_snow.sel(basin=b).values)
        rows.append({
            "id": b, "name": (st["STANAME"] if st is not None else b), "state": st["STATE"] if st is not None else None,
            "lat": round(float(cube.lat.sel(basin=b).values), 4), "lon": round(float(cube.lon.sel(basin=b).values), 4),
            "area_km2": round(float(cube.area_km2.sel(basin=b).values), 1), "snow": round(snow, 3) if np.isfinite(snow) else None,
            "rb": round(rb, 3) if np.isfinite(rb) else None, "below_dam": bool(cube.below_dam.sel(basin=b).values > 0),
            "q": q, "t": t, "pct": None if pct is None else round(pct, 3),
            "featured": SITES[b]["slug"] if b in SITES else None,
        })
    write(out / "sites.json", {"generated_at": now.isoformat(), "sites": rows, "featured": [{"id": k, **v} for k, v in SITES.items()]})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "public/data")
    ap.add_argument("--cache", type=Path, default=Path("/tmp/lp-cache"))
    ap.add_argument("--calibration", type=Path, default=DEFAULT_CAL, help="PR #51 standard-path params dir (with flow/ and temperature/)")
    ap.add_argument("--sites", nargs="*", default=list(SITES))
    ap.add_argument("--skip-overview", action="store_true")
    ap.add_argument("--skip-geo", action="store_true")
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    cube = open_cube()
    sel = selection()
    usgs = WaterDataClient()
    for site in args.sites:
        build_site(site, SITES[site], cube, sel, args.calibration, args.cache, args.out, usgs, args.skip_geo)
    if not args.skip_overview:
        build_overview(cube, sel, args.calibration, args.out, usgs, args.cache)


if __name__ == "__main__":
    main()
