"""Area-weighted extraction of gridded forcings to basins, sharded for a fleet of Spot instances.

A *plan* (one per source) holds per-tile dense weight matrices, the unit list and the shard layout.
A *shard* is a span of the source's leading dimension (a year of AORC, a year of HRRR forecast inits,
a quarter of GEFS inits, ...). Work inside a shard is (block x tile) tasks, where a tile is one source
chunk footprint, so every read is chunk-aligned.

Each instance runs its shards in two phases: first the tiles touching the early-slice basins, then the
rest. After phase A the slice basins are complete for every shard the instance owns, so their outputs
are uploaded immediately; phase B then completes every other basin without re-reading anything.

Output per shard (S3 `work/extract/<source>/<shard>/{slice,all}.npy.zst` + `.json`): float32
(units, *leading, variables), the area-weighted mean over valid cells, NaN where less than half the
unit's weight had valid data.
"""

import io
import itertools
import json
import logging
import multiprocessing
import os
import pickle
import resource
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
import zstandard

from . import grids, sources
from .sources import SOURCES, Source

log = logging.getLogger(__name__)

MIN_VALID_WEIGHT = 0.5


@dataclass
class Tile:
    tile_id: int
    iy0: int
    iy1: int
    ix0: int
    ix1: int
    cells: np.ndarray  # flat index inside the tile window
    units: np.ndarray  # plan unit indices
    weights_t: sp.csr_matrix  # (len(units), len(cells)) float32; transposed so x @ W is W.T @ x.T
    wsum: np.ndarray  # (len(units),) summed weight of this tile per unit


@dataclass
class Shard:
    shard_id: str
    year: int | None  # AORC store year
    start: int  # leading-dim index range [start, stop) in the source (or year) store
    stop: int
    coords: np.ndarray  # datetime64 of the leading dim (time or init_time)


@dataclass
class Plan:
    source: str
    units: list[str]
    slice_units: np.ndarray
    wsum: np.ndarray
    tiles: list[Tile]
    slice_tiles: set[int]
    shards: list[Shard]
    lead_coords: np.ndarray | None = None  # timedelta64 leads for forecast sources

    def save(self, path: Path) -> None:
        path.write_bytes(pickle.dumps(self, protocol=5))

    @staticmethod
    def load(path: Path) -> "Plan":
        return pickle.loads(path.read_bytes())


def build_tiles(w: sp.csr_matrix, grid: grids.Grid) -> list[Tile]:
    coo = w.tocoo()
    iy, ix = np.divmod(coo.col, grid.nx)
    tile = grid.tile_of(iy, ix)
    df = pd.DataFrame({"tile": tile, "iy": iy, "ix": ix, "unit": coo.row, "w": coo.data})
    out = []
    for tid, g in df.groupby("tile"):
        ty, tx = divmod(int(tid), grid.tiles_x)
        iy0, ix0 = ty * grid.chunk_y, tx * grid.chunk_x
        iy1, ix1 = min(iy0 + grid.chunk_y, grid.ny), min(ix0 + grid.chunk_x, grid.nx)
        local = (g["iy"].to_numpy() - iy0) * (ix1 - ix0) + (g["ix"].to_numpy() - ix0)
        cells, cell_pos = np.unique(local, return_inverse=True)
        units, unit_pos = np.unique(g["unit"].to_numpy(), return_inverse=True)
        wt = sp.csr_matrix((g["w"].to_numpy(np.float32), (unit_pos, cell_pos)), shape=(len(units), len(cells)))
        out.append(Tile(int(tid), iy0, iy1, ix0, ix1, cells, units, wt, np.asarray(wt.sum(axis=1)).ravel().astype(np.float32)))
    return out


def analysis_shards(src: Source, start: pd.Timestamp, end: pd.Timestamp) -> list[Shard]:
    if src.bucket == sources.AORC_BUCKET:
        shards = []
        for year in range(start.year, end.year + 1):
            try:
                n = sources.aorc_group(year)["time"].shape[0]
            except (FileNotFoundError, KeyError):
                continue
            coords = pd.date_range(f"{year}-01-01", periods=n, freq="h").to_numpy()
            shards.append(Shard(f"{year}", year, 0, n, coords))
        return shards
    g = sources.group_for(src)
    t = g["time"]
    times = pd.to_datetime(t[:], unit=_time_unit(t)).to_numpy()
    lo = int(np.searchsorted(times, np.datetime64(start.tz_convert(None))))
    hi = int(np.searchsorted(times, np.datetime64(end.tz_convert(None)), side="right"))
    lo -= lo % src.block
    span = src.block * max(1, 8760 // src.block)
    return [
        Shard(pd.Timestamp(times[s]).strftime("%Y%m%d%H"), None, s, min(s + span, hi), times[s : min(s + span, hi)])
        for s in range(lo, hi, span)
    ]


def forecast_shards(src: Source, start: pd.Timestamp, end: pd.Timestamp, inits_per_shard: int) -> tuple[list[Shard], np.ndarray]:
    g = sources.group_for(src)
    it = g["init_time"]
    inits = pd.to_datetime(it[:], unit=_time_unit(it)).to_numpy()
    lt = g["lead_time"]
    leads = pd.to_timedelta(lt[:], unit=_time_unit(lt)).to_numpy()[: src.leads]
    lo = int(np.searchsorted(inits, np.datetime64(start.tz_convert(None))))
    hi = int(np.searchsorted(inits, np.datetime64(end.tz_convert(None)), side="right"))
    shards = [
        Shard(pd.Timestamp(inits[s]).strftime("%Y%m%d%H"), None, s, min(s + inits_per_shard, hi), inits[s : min(s + inits_per_shard, hi)])
        for s in range(lo, hi, inits_per_shard)
    ]
    return shards, leads


def _time_unit(arr) -> str:
    units = str(arr.attrs.get("units", "seconds"))
    return {"seconds": "s", "microseconds": "us", "hours": "h", "days": "D", "minutes": "m"}[units.split(" ")[0]]


def make_plan(src_name: str, w: sp.csr_matrix, units: list[str], slice_units: np.ndarray, start: pd.Timestamp, end: pd.Timestamp) -> Plan:
    src = SOURCES[src_name]
    grid = sources.grid_for(src)
    tiles = build_tiles(w, grid)
    slice_set = set(slice_units.tolist())
    slice_tiles = {t.tile_id for t in tiles if slice_set & set(t.units.tolist())}
    if src.kind == "analysis":
        shards, leads = analysis_shards(src, start, end), None
    else:
        per = 1460 if src.name == "hrrr_forecast" else 92
        shards, leads = forecast_shards(src, start, end, per)
    return Plan(src_name, units, np.asarray(slice_units), np.asarray(w.sum(axis=1)).ravel().astype(np.float32), tiles, slice_tiles, shards, leads)


# ----------------------------------------------------------------------------------- workers

_PLANS: dict[str, Plan] = {}


def _init_worker(plan_paths: dict[str, str]) -> None:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    for name, path in plan_paths.items():
        if name not in _PLANS:
            _PLANS[name] = Plan.load(Path(path))


def _reduce(x: np.ndarray, tile: Tile) -> np.ndarray:
    """(..., cells) -> (..., units) weighted sums."""
    lead = x.shape[:-1]
    flat = x.reshape(-1, x.shape[-1])
    return np.asarray(tile.weights_t @ flat.T).T.reshape(*lead, len(tile.units)).astype(np.float32, copy=False)


def leading_shape(src: Source, n: int) -> tuple[int, ...]:
    if src.kind == "analysis":
        return (n,)
    return (n, src.members, src.leads) if src.members else (n, src.leads)


def _index(src: Source, b0: int, b1: int, tile: Tile) -> tuple:
    ys, xs = slice(tile.iy0, tile.iy1), slice(tile.ix0, tile.ix1)
    if src.kind == "analysis":
        return (slice(b0, b1), ys, xs)
    if src.members:
        return (slice(b0, b1), slice(0, src.members), slice(0, src.leads), ys, xs)
    return (slice(b0, b1), slice(0, src.leads), ys, xs)


def run_task(source: str, shard_i: int, b0: int, b1: int, tile_i: int):
    plan = _PLANS[source]
    src = SOURCES[source]
    shard, tile = plan.shards[shard_i], plan.tiles[tile_i]
    group = sources.group_for(src, shard.year)
    idx = _index(src, b0, b1, tile)
    with ThreadPoolExecutor(len(src.raw_vars)) as io_pool:
        raw = dict(zip(src.raw_vars, io_pool.map(lambda v: sources.read_raw(group[v], idx), src.raw_vars)))
    conv = src.convert(raw)
    lead_shape = next(iter(raw.values())).shape[:-2]
    n_cells_window = (tile.iy1 - tile.iy0) * (tile.ix1 - tile.ix0)
    num = np.empty((*lead_shape, len(tile.units), len(src.outputs)), dtype=np.float32)
    den = None  # NaN entries mean "every cell valid" for that variable
    for k, name in enumerate(src.outputs):
        x = conv[name].reshape(*lead_shape, n_cells_window)[..., tile.cells]
        if src.strict:
            num[..., k] = _reduce(x, tile)
            continue
        bad = np.isnan(x)
        if bad.any():
            if den is None:
                den = np.full(num.shape, np.nan, dtype=np.float32)
            den[..., k] = _reduce((~bad).astype(np.float32), tile)
            x = np.where(bad, np.float32(0.0), x)
        num[..., k] = _reduce(x, tile)
    return source, shard_i, b0, tile_i, num, den


# ----------------------------------------------------------------------------------- driver


class ShardAccumulator:
    def __init__(self, plan: Plan, shard_i: int):
        src = SOURCES[plan.source]
        shard = plan.shards[shard_i]
        shape = (*leading_shape(src, shard.stop - shard.start), len(plan.units), len(src.outputs))
        self.plan, self.shard_i, self.src = plan, shard_i, src
        self.num = np.zeros(shape, dtype=np.float32)
        self.den = None if src.strict else np.zeros(shape, dtype=np.float32)

    def add(self, b0: int, tile_i: int, num: np.ndarray, den: np.ndarray | None) -> None:
        tile = self.plan.tiles[tile_i]
        lo = b0 - self.plan.shards[self.shard_i].start
        sl = slice(lo, lo + num.shape[0])
        self.num[sl][..., tile.units, :] += num
        if self.den is None:
            return
        wsum = tile.wsum[:, None]
        if den is None:
            self.den[sl][..., tile.units, :] += wsum
        else:
            full = np.where(np.isnan(den), wsum, den)
            self.den[sl][..., tile.units, :] += full

    def values(self, units: np.ndarray) -> np.ndarray:
        num = self.num[..., units, :]
        if self.den is None:
            out = (num / self.plan.wsum[units][:, None]).astype(np.float32)
            return np.moveaxis(out, -2, 0)
        den = self.den[..., units, :]
        need = MIN_VALID_WEIGHT * self.plan.wsum[units][:, None]
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(den >= need, num / den, np.nan).astype(np.float32)
        return np.moveaxis(out, -2, 0)  # (units, *leading, vars)


def tasks_for(plan: Plan, shard_i: int, phase: str) -> list[tuple]:
    src = SOURCES[plan.source]
    shard = plan.shards[shard_i]
    tiles = [i for i, t in enumerate(plan.tiles) if (t.tile_id in plan.slice_tiles) == (phase == "A")]
    return [
        (plan.source, shard_i, b0, min(b0 + src.block, shard.stop), ti)
        for b0 in range(shard.start, shard.stop, src.block)
        for ti in tiles
    ]


def write_output(path: Path, values: np.ndarray, meta: dict) -> None:
    buf = io.BytesIO()
    np.save(buf, values, allow_pickle=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor(level=3, threads=-1).compress(buf.getvalue()))
    path.with_suffix("").with_suffix(".json").write_text(json.dumps(meta))


def read_output(path: Path) -> np.ndarray:
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes(), max_output_size=1 << 40)
    return np.load(io.BytesIO(raw), allow_pickle=False)


def run_jobs(jobs: list[tuple[str, int]], plan_paths: dict[str, str], out_dir: Path, workers: int, upload=None) -> None:
    """Run (source, shard index) jobs in two phases on this machine; `upload(path)` ships each output."""
    _init_worker(plan_paths)
    plans = _PLANS
    accs = {(s, i): ShardAccumulator(plans[s], i) for s, i in jobs}
    # icechunk's async runtime is not fork-safe, so workers are spawned and load the (sparse) plans themselves.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init_worker, initargs=(plan_paths,)) as pool:
        for phase in ("A", "B"):
            tasks = [t for s, i in jobs for t in tasks_for(plans[s], i, phase)]
            log.info("phase %s: %d tasks", phase, len(tasks))
            t0 = last_log = time.time()
            done = 0
            # Bounded in-flight window: finished futures are dropped as soon as they're accumulated, so memory
            # stays at the accumulators plus a few task results.
            queue = iter(tasks)
            in_flight = {pool.submit(run_task, *t) for t in itertools.islice(queue, 4 * workers)}
            while in_flight:
                finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    source, shard_i, b0, tile_i, num, den = fut.result()
                    accs[(source, shard_i)].add(b0, tile_i, num, den)
                    del num, den
                    nxt = next(queue, None)
                    if nxt is not None:
                        in_flight.add(pool.submit(run_task, *nxt))
                    done += 1
                if time.time() - last_log > 60 or done == len(tasks):
                    last_log = time.time()
                    rate = done / (last_log - t0)
                    rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
                    log.info("phase %s %d/%d tasks (%.1f/s, eta %.0f min, driver peak rss %.1f GB)", phase, done, len(tasks), rate, (len(tasks) - done) / rate / 60, rss_gb)
            for (s, i), acc in accs.items():
                plan, shard = plans[s], plans[s].shards[i]
                units = plan.slice_units if phase == "A" else np.arange(len(plan.units))
                if len(units) == 0:
                    continue
                meta = {
                    "source": s,
                    "shard": shard.shard_id,
                    "phase": phase,
                    "units": [plan.units[u] for u in units],
                    "variables": list(SOURCES[s].outputs),
                    "leading": [str(c) for c in pd.to_datetime(shard.coords)],
                    "leads_h": None if plan.lead_coords is None else (plan.lead_coords / np.timedelta64(1, "h")).tolist(),
                    "members": SOURCES[s].members,
                }
                path = out_dir / s / shard.shard_id / ("slice.npy.zst" if phase == "A" else "all.npy.zst")
                write_output(path, acc.values(units), meta)
                if upload:
                    upload(path)
                    upload(path.with_suffix("").with_suffix(".json"))
                if phase == "B":
                    acc.num = acc.den = None
