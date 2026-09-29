"""On-disk cache of the streaming dataset's sample index and normalization statistics.

Indexing reads every basin once (about 15 min for the full cube on a g5.xlarge), and a Spot run repeats it on every
boot. The cache is one file per dataset in the run directory, which is synced to S3 with the checkpoints. Its key
covers everything the index and statistics depend on: the cube stores (root metadata, a sibling MANIFEST_ID if
present, and the file sizes of every array read), the basin list, the period and its per-basin dates, the config
fields that shape samples, and the dataset options. A different key, or any error reading the file, means the index
is rebuilt, so a stale cache is never used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import torch

if TYPE_CHECKING:  # the dataset module imports this one
    from .dataset import ZarrCubeDataset

LOGGER = logging.getLogger(__name__)
CACHE_VERSION = 1
CACHE_DIR = "index_cache"

# Config fields that decide which samples are valid, their windows and the statistics.
SAMPLING_CFG_FIELDS = (
    "seq_length", "predict_last_n", "forecast_seq_length", "forecast_overlap", "lagged_features", "duplicate_features",
    "dynamic_inputs", "hindcast_inputs", "forecast_inputs", "target_variables", "evolving_attributes", "mass_inputs",
    "autoregressive_inputs", "dynamic_conceptual_inputs", "custom_normalization", "clip_targets_to_zero", "loss",
    "use_frequencies", "nan_handling_method", "train_start_date", "train_end_date", "validation_start_date",
    "validation_end_date", "test_start_date", "test_end_date",
)
# Dataset options that only size blocks and caches or pick evaluation members.
NON_SAMPLING_OPTIONS = {"block_basins", "chunk_samples", "cache_basins", "forecast_float16", "forecast_member"}


def _store_fingerprint(path: str | Path, arrays: set[str]) -> dict | None:
    """Identity of a local Zarr store; None (no caching) for remote or non-Zarr stores."""
    root = Path(path)
    if "://" in str(path) or not root.is_dir():
        return None
    meta = next((root / n for n in ("zarr.json", ".zmetadata") if (root / n).is_file()), None)
    if meta is None:
        return None
    sizes = {}
    for name in sorted(arrays):
        d = root / name
        if d.is_dir():
            sizes[name] = sorted((str(p.relative_to(d)), p.stat().st_size) for p in d.rglob("*") if p.is_file())
    manifest = root.parent / "MANIFEST_ID"
    return {
        "metadata": hashlib.sha256(meta.read_bytes()).hexdigest(),
        "manifest_id": manifest.read_text().strip() if manifest.is_file() else None,
        "arrays": hashlib.sha256(json.dumps(sizes).encode()).hexdigest(),
    }


def cache_key(ds: ZarrCubeDataset) -> str | None:
    """Key of the dataset's index, or None when it can't be cached (remote store, single basin, no run directory)."""
    if ds.cfg.run_dir is None or len(ds.basins) < 2:
        return None
    cube = ds._cube
    arrays: dict[int, set[str]] = {}
    for feature in [*ds._cube_columns, *ds._forecast_sources]:
        for ref in cube._refs.get(feature, []):
            arrays.setdefault(ref.store, {cube.dims.basin, cube.dims.time}).add(ref.var)
    stores = [_store_fingerprint(p, arrays.get(i, {cube.dims.basin, cube.dims.time})) for i, p in enumerate(cube.paths)]
    if any(s is None for s in stores):
        return None
    cfg = ds.cfg.as_dict()
    payload = {
        "version": CACHE_VERSION,
        "neuralhydrology": version("neuralhydrology"),
        "period": ds.period,
        "is_train": ds.is_train,
        "compute_scaler": bool(ds._compute_scaler),
        "basins": list(ds.basins),
        "dates": {b: ds.start_and_end_dates.get(b) for b in ds.basins},
        "cfg": {k: cfg.get(k) for k in SAMPLING_CFG_FIELDS},
        "options": {k: v for k, v in asdict(ds.options).items() if k not in NON_SAMPLING_OPTIONS},
        "columns": ds._cube_columns,
        "forecast_sources": ds._forecast_sources,
        "stores": stores,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def cache_path(ds: ZarrCubeDataset, key: str) -> Path:
    return Path(ds.cfg.run_dir) / CACHE_DIR / f"{ds.period}-{key[:24]}.npz"


def _ranges(idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Valid end positions as runs of consecutive values: (starts, lengths)."""
    if idx.size == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    breaks = np.flatnonzero(np.diff(idx) != 1) + 1
    starts = idx[np.concatenate([[0], breaks])]
    lengths = np.diff(np.concatenate([[0], breaks, [idx.size]]))
    return starts.astype(np.int64), lengths.astype(np.int64)


def write(ds: ZarrCubeDataset, key: str, basins: list[str], valid: list[np.ndarray]) -> None:
    path = cache_path(ds, key)
    arrays: dict[str, np.ndarray] = {"key": np.array(key), "basins": np.array(basins, dtype=str)}
    # NeuralHydrology fills these in while loading the first basin (native frequency, per-frequency lists)
    arrays["nh_state"] = np.array(json.dumps({"frequencies": ds.frequencies, "seq_len": ds.seq_len, "predict_last_n": ds._predict_last_n}))
    runs = [_ranges(v) for v in valid]
    arrays["n_runs"] = np.array([len(s) for s, _ in runs], dtype=np.int64)
    arrays["run_starts"] = np.concatenate([s for s, _ in runs]) if runs else np.empty(0, np.int64)
    arrays["run_lengths"] = np.concatenate([n for _, n in runs]) if runs else np.empty(0, np.int64)
    if ds._compute_scaler:
        center, scale = ds.scaler["xarray_feature_center"], ds.scaler["xarray_feature_scale"]
        names = list(center.data_vars)
        arrays["scaler_names"] = np.array(names, dtype=str)
        arrays["scaler_center"] = np.array([center[n].values for n in names], dtype=np.float32)
        arrays["scaler_scale"] = np.array([scale[n].values for n in names], dtype=np.float32)
    stds = ds._per_basin_target_stds
    if stds:
        arrays["std_basins"] = np.array(list(stds), dtype=str)
        arrays["std_values"] = np.stack([stds[b].numpy()[0] for b in stds]).astype(np.float32)
    if ds.period_starts:
        arrays["start_basins"] = np.array(list(ds.period_starts), dtype=str)
        arrays["start_ns"] = np.array([pd.Timestamp(t).value for t in ds.period_starts.values()], dtype=np.int64)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}.npz")
        np.savez(tmp, **arrays)
        os.replace(tmp, path)
        LOGGER.info("wrote index cache %s", path)
    except OSError as err:
        LOGGER.warning("could not write the index cache %s: %s", path, err)


def read(ds: ZarrCubeDataset, key: str) -> tuple[list[str], list[np.ndarray], dict] | None:
    """(basins, valid end positions, extras) from the cache, or None to rebuild."""
    path = cache_path(ds, key)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            if str(z["key"]) != key:
                LOGGER.info("index cache %s has a different key; rebuilding", path)
                return None
            basins = [str(b) for b in z["basins"]]
            n_runs, starts, lengths = z["n_runs"], z["run_starts"], z["run_lengths"]
            if len(n_runs) != len(basins) or n_runs.sum() != len(starts) or len(starts) != len(lengths):
                raise ValueError("inconsistent run arrays")
            bounds = np.concatenate([[0], np.cumsum(n_runs)])
            valid = [
                np.concatenate([np.arange(s, s + n, dtype=np.int32) for s, n in zip(starts[a:b], lengths[a:b])]) if b > a else np.empty(0, np.int32)
                for a, b in zip(bounds[:-1], bounds[1:])
            ]
            extras: dict = {"nh_state": json.loads(str(z["nh_state"]))}
            if "scaler_names" in z.files:
                extras["scaler"] = ([str(n) for n in z["scaler_names"]], z["scaler_center"], z["scaler_scale"])
            if "std_basins" in z.files:
                extras["stds"] = {str(b): torch.tensor(v[None, :]) for b, v in zip(z["std_basins"], z["std_values"])}
            if "start_basins" in z.files:
                extras["period_starts"] = {str(b): pd.Timestamp(int(t)) for b, t in zip(z["start_basins"], z["start_ns"])}
        if ds._compute_scaler and "scaler" not in extras:
            raise ValueError("cache has no scaler")
        return basins, valid, extras
    except Exception as err:  # any unreadable or inconsistent cache is rebuilt
        LOGGER.warning("could not read the index cache %s (%s); rebuilding", path, err)
        return None
