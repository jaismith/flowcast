"""Live forcing extraction for active sites: HRRR analysis, MRMS and GEFS basin means from dynamical.org.

The weights are the training cube's own (the `flowcast-dataset` extraction plans, `work/runs/r1/plans/*.pkl` and
`rf1/plans/gefs_forecast_bands.pkl`), cut down at onboarding to one site's units and tiles
(`sites/USGS-{id}/serving/weights/{source}.npz`), and the reduction is the plan's: weighted mean over valid cells,
NaN where less than half the unit's weight is valid; forecasts strict (any missing cell -> NaN). So a live basin mean
is the number the cube would hold for that hour (gate 2, `flowcast-serve parity-extract`).

Each store is chunked over long time spans (HRRR analysis 90 days, MRMS 27 days), so a run reads whole chunks no
matter how few hours it needs; it extracts the full hindcast window every run and archives the result
(`forcing/{product}/{site}/{YYYY-MM}.parquet`, `forcing/gefs/{init}/{site}.parquet`). Tiles shared by several sites
are read once per run.
"""

from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd
from flowcast_pipeline.dataset import sources
from flowcast_pipeline.dataset.extract import MIN_VALID_WEIGHT
from flowcast_pipeline.dataset.sources import SOURCES
from flowcast_pipeline.lake import Lake

log = logging.getLogger(__name__)

# product prefix in the cube -> extraction source
ANALYSIS = {"hrrr_an": "hrrr_analysis", "mrms": "mrms"}
GEFS_SOURCES = {"gefs": "gefs_forecast", "gefs_band": "gefs_forecast_bands"}
GEFS_LEADS = 64  # 0-189 h at 3 h, the cube's lead grid
GEFS_MEMBERS = 11
N_BANDS = 4


@dataclass
class SiteWeights:
    """One site's slice of an extraction plan: per source tile, the cells read and the dense unit x cell weights."""

    source: str
    units: list[str]  # e.g. ["01427510"] or ["01427510/band0", ...]
    wsum: np.ndarray  # (units,) the unit's total weight over all tiles
    tiles: list[tuple[int, int, int, int, int, np.ndarray, np.ndarray]]  # (tile id, iy0, iy1, ix0, ix1, cells, W[units, cells])

    def to_bytes(self) -> bytes:
        arrays = {"wsum": self.wsum}
        meta = {"source": self.source, "units": self.units, "tiles": []}
        for k, (tid, iy0, iy1, ix0, ix1, cells, w) in enumerate(self.tiles):
            meta["tiles"].append([tid, iy0, iy1, ix0, ix1])
            arrays[f"cells{k}"], arrays[f"w{k}"] = cells, w
        buf = io.BytesIO()
        np.savez_compressed(buf, meta=np.array(json.dumps(meta)), **arrays)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> SiteWeights:
        with np.load(io.BytesIO(data)) as f:
            meta = json.loads(str(f["meta"]))
            tiles = [(int(t[0]), int(t[1]), int(t[2]), int(t[3]), int(t[4]), f[f"cells{k}"], f[f"w{k}"]) for k, t in enumerate(meta["tiles"])]
            return cls(meta["source"], meta["units"], f["wsum"], tiles)

    @classmethod
    def from_plan(cls, plan, basin: str) -> SiteWeights:
        """The units of `basin` (the basin itself, or its `/band{k}` units) out of a `flowcast_pipeline` extraction Plan."""
        wanted = [i for i, u in enumerate(plan.units) if u == basin or u.startswith(f"{basin}/")]
        if not wanted:
            raise KeyError(f"basin {basin} is not in the {plan.source} plan")
        pos = {u: j for j, u in enumerate(wanted)}
        tiles = []
        for t in plan.tiles:
            rows = [r for r, u in enumerate(t.units.tolist()) if u in pos]
            if not rows:
                continue
            w = t.weights_t[rows].toarray().astype(np.float32)
            used = np.flatnonzero(w.any(axis=0))
            dense = np.zeros((len(wanted), len(used)), dtype=np.float32)
            dense[[pos[int(t.units[r])] for r in rows]] = w[:, used]
            tiles.append((t.tile_id, t.iy0, t.iy1, t.ix0, t.ix1, t.cells[used].astype(np.int64), dense))
        return cls(plan.source, [plan.units[i] for i in wanted], plan.wsum[wanted].astype(np.float32), tiles)


def weights_key(basin: str, source: str) -> str:
    return f"sites/USGS-{basin}/serving/weights/{source}.npz"


def load_weights(lake: Lake, basin: str, source: str) -> SiteWeights:
    data = lake.read(weights_key(basin, source))
    if data is None:
        raise FileNotFoundError(f"{weights_key(basin, source)} is missing; run `flowcast-serve onboard`")
    return SiteWeights.from_bytes(data)


def _time_index(group, name: str) -> pd.DatetimeIndex:
    arr = group[name]
    unit = str(arr.attrs.get("units", "seconds")).split(" ")[0]
    return pd.DatetimeIndex(pd.to_datetime(arr[:], unit={"seconds": "s", "hours": "h", "days": "D", "minutes": "m"}[unit])).tz_localize("UTC")


def latest_time(source: str) -> pd.Timestamp:
    g = sources.group_for(SOURCES[source])
    return _time_index(g, "time" if SOURCES[source].kind == "analysis" else "init_time")[-1]


def _read_tile(group, src, index: tuple) -> dict[str, np.ndarray]:
    with ThreadPoolExecutor(len(src.raw_vars)) as pool:
        raw = dict(zip(src.raw_vars, pool.map(lambda v: sources.read_raw(group[v], index), src.raw_vars)))
    return src.convert(raw)


def extract_analysis(source: str, sites: dict[str, SiteWeights], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Hourly basin means over [start, end] per site: {basin: frame indexed by UTC time, columns = outputs}."""
    src = SOURCES[source]
    group = sources.group_for(src)
    times = _time_index(group, "time")
    lo, hi = int(times.searchsorted(start)), int(times.searchsorted(end, side="right"))
    index = times[lo:hi]
    nv = len(src.outputs)
    acc = {b: (np.zeros((len(index), len(w.units), nv), np.float64), np.zeros((len(index), len(w.units), nv), np.float64)) for b, w in sites.items()}
    by_tile: dict[int, list[tuple[str, int]]] = {}
    for b, w in sites.items():
        for k, t in enumerate(w.tiles):
            by_tile.setdefault(t[0], []).append((b, k))

    def run(tid: int):
        b0, k0 = by_tile[tid][0]
        _, iy0, iy1, ix0, ix1, _, _ = sites[b0].tiles[k0]
        conv = _read_tile(group, src, (slice(lo, hi), slice(iy0, iy1), slice(ix0, ix1)))
        return tid, {name: conv[name].reshape(hi - lo, -1) for name in src.outputs}

    with ThreadPoolExecutor(4) as pool:
        for tid, fields in pool.map(run, list(by_tile)):
            for b, k in by_tile[tid]:
                _, _, _, _, _, cells, w = sites[b].tiles[k]
                num, den = acc[b]
                for j, name in enumerate(src.outputs):
                    x = fields[name][:, cells].astype(np.float64)
                    ok = np.isfinite(x)
                    num[:, :, j] += np.where(ok, x, 0.0) @ w.T
                    den[:, :, j] += ok.astype(np.float64) @ w.T
    out = {}
    for b, w in sites.items():
        num, den = acc[b]
        with np.errstate(invalid="ignore", divide="ignore"):
            v = np.where(den >= MIN_VALID_WEIGHT * w.wsum[None, :, None], num / den, np.nan)
        out[b] = pd.DataFrame(v[:, 0, :].astype(np.float32), index=index, columns=list(src.outputs))
    return out


def gefs_init_for(issue: pd.Timestamp, latency_h: float = 6.0, max_age_h: float = 30.0) -> pd.Timestamp | None:
    """The newest 00Z init in the store at least `latency_h` before the issue (the training rule), if <= `max_age_h` old."""
    inits = _time_index(sources.group_for(SOURCES["gefs_forecast"]), "init_time")
    ok = inits[inits <= issue - pd.Timedelta(hours=latency_h)]
    if not len(ok) or issue - ok[-1] > pd.Timedelta(hours=max_age_h):
        return None
    return ok[-1]


def extract_gefs(source: str, sites: dict[str, SiteWeights], init: pd.Timestamp) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Per site: values[unit, member, lead, output] for one init (members 0-10, leads 0-189 h); and the leads (h)."""
    src = SOURCES[source]
    group = sources.group_for(src)
    inits = _time_index(group, "init_time")
    i = int(inits.get_loc(init))
    lt = group["lead_time"]
    unit = str(lt.attrs.get("units", "seconds")).split(" ")[0]
    leads = (pd.to_timedelta(lt[:GEFS_LEADS], unit={"seconds": "s", "hours": "h"}[unit]) / pd.Timedelta(hours=1)).to_numpy(float)
    nv = len(src.outputs)
    acc = {b: np.zeros((len(w.units), GEFS_MEMBERS, GEFS_LEADS, nv), np.float64) for b, w in sites.items()}
    by_tile: dict[int, list[tuple[str, int]]] = {}
    for b, w in sites.items():
        for k, t in enumerate(w.tiles):
            by_tile.setdefault(t[0], []).append((b, k))

    def run(tid: int):
        b0, k0 = by_tile[tid][0]
        _, iy0, iy1, ix0, ix1, _, _ = sites[b0].tiles[k0]
        conv = _read_tile(group, src, (slice(i, i + 1), slice(0, GEFS_MEMBERS), slice(0, GEFS_LEADS), slice(iy0, iy1), slice(ix0, ix1)))
        return tid, {name: conv[name].reshape(GEFS_MEMBERS, GEFS_LEADS, -1) for name in src.outputs}

    with ThreadPoolExecutor(4) as pool:
        for tid, fields in pool.map(run, list(by_tile)):
            for b, k in by_tile[tid]:
                _, _, _, _, _, cells, w = sites[b].tiles[k]
                for j, name in enumerate(src.outputs):
                    acc[b][..., j] += np.moveaxis(fields[name][:, :, cells].astype(np.float64) @ w.T, -1, 0)
    return {b: (acc[b] / sites[b].wsum[:, None, None, None]).astype(np.float32) for b in sites}, leads


# ---------------------------------------------------------------------------------------------- archive


def analysis_key(prefix: str, basin: str, month: str) -> str:
    return f"forcing/{prefix}/USGS-{basin}/{month}.parquet"


def store_analysis(lake: Lake, prefix: str, basin: str, frame: pd.DataFrame) -> None:
    frame = frame.dropna(how="all")
    if frame.empty:
        return
    df = frame.rename_axis("time").reset_index()
    for month, part in df.groupby(df["time"].dt.strftime("%Y-%m")):
        k = analysis_key(prefix, basin, month)
        old = lake.read_parquet(k)
        if old is not None:
            part = pd.concat([old[~old["time"].isin(part["time"])], part], ignore_index=True)
        lake.write_parquet(k, part.sort_values("time").reset_index(drop=True))


def load_analysis(lake: Lake, prefix: str, basin: str, start: pd.Timestamp, end: pd.Timestamp, columns: list[str]) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="h", tz="UTC")
    frames = [f for f in (lake.read_parquet(analysis_key(prefix, basin, str(m))) for m in pd.period_range(start.tz_convert(None), end.tz_convert(None), freq="M")) if f is not None]
    if not frames:
        return pd.DataFrame(np.nan, index=index, columns=columns, dtype=np.float32)
    df = pd.concat(frames).drop_duplicates("time", keep="last").set_index("time")
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
    return df.reindex(index)[columns].astype(np.float32)


def gefs_key(init: pd.Timestamp, basin: str) -> str:
    return f"forcing/gefs/{init:%Y%m%d%H}/USGS-{basin}.npz"


def store_gefs(lake: Lake, init: pd.Timestamp, basin: str, basin_values: np.ndarray, band_values: np.ndarray | None, leads: np.ndarray) -> None:
    buf = io.BytesIO()
    arrays = {"basin": basin_values, "leads": leads}
    if band_values is not None:
        arrays["bands"] = band_values
    np.savez_compressed(buf, **arrays)
    lake.write(gefs_key(init, basin), buf.getvalue(), "application/octet-stream")


def load_gefs(lake: Lake, init: pd.Timestamp, basin: str) -> dict[str, np.ndarray] | None:
    data = lake.read(gefs_key(init, basin))
    if data is None:
        return None
    with np.load(io.BytesIO(data)) as f:
        return {k: f[k] for k in f.files}
