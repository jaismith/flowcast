"""Streaming Zarr dataset for NeuralHydrology (rebuild plan §5.2, milestone 1.4).

`ZarrCubeDataset` is registered as NeuralHydrology dataset `flowcast_zarr`. It keeps NeuralHydrology's own
per-basin preprocessing (period slicing, warmup, lagged/duplicated features, sample validation) and its
`__getitem__` slicing, so a sample is built exactly as the stock loader builds it. What changes:

* Basins are read one at a time from the cube instead of all at once into one in-memory xarray.
* Normalization statistics and per-basin target stds are accumulated in one streaming pass.
* The sample index is one int32 array of valid end positions per basin, not a Python dict of tuples.
* Basin arrays are loaded on demand into a small per-process LRU cache; `BasinBlockBatchSampler` draws batches
  from K basins at a time so each DataLoader worker only holds about K basins.

Additions the stock loader lacks:

* `optional_inputs`: inputs allowed to be missing (masked by the model's `nan_handling_method`), so e.g. lagged
  observed flow or a below-dam outflow gauge doesn't invalidate a sample.
* `group_dropout`: per-feature-group probability of masking the whole hindcast group during training (plan §3:
  lagged observed flow masked about 50% of the time).
* Archived forecast forcing (`forecast[basin, issue_time, lead, member]`): forecast-branch inputs come from the
  latest issue available at the sample's issue time, not from the future of the hindcast series.
"""

from __future__ import annotations

import logging
import sys
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import torch
import xarray
from neuralhydrology.datasetzoo import register_dataset
from neuralhydrology.datasetzoo.basedataset import BaseDataset, _validate_samples
from neuralhydrology.utils.config import Config
from neuralhydrology.utils.errors import NoEvaluationDataError, NoTrainDataError
from torch.utils.data import Sampler
from tqdm import tqdm

from .cube import Cube, CubeDims, FROZEN_TEST_START

LOGGER = logging.getLogger(__name__)


@dataclass
class DatasetOptions:
    """Flowcast-specific dataset options (the `flowcast.dataset` block of a training config)."""

    cube: list[str] = field(default_factory=list)
    dims: dict = field(default_factory=dict)
    optional_inputs: list[str] = field(default_factory=list)
    group_dropout: dict[str, float] = field(default_factory=dict)
    block_basins: int = 16
    cache_basins: int = 0
    forecast_latency_h: dict[str, float] = field(default_factory=dict)
    allow_frozen_test: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "DatasetOptions":
        d = dict(d or {})
        if isinstance(d.get("cube"), str):
            d["cube"] = [d["cube"]]
        return cls(**d)


class _LRU(OrderedDict):
    def __init__(self, capacity: int, load):
        super().__init__()
        self.capacity = max(1, capacity)
        self._load = load

    def __missing__(self, key):
        value = self._load(key)
        self[key] = value
        while len(self) > self.capacity:
            self.popitem(last=False)
        return value

    def __getitem__(self, key):
        if key in self:
            self.move_to_end(key)
        return super().__getitem__(key)


class _View:
    """Dict-like view of one field of the cached basin blocks (what BaseDataset.__getitem__ indexes)."""

    def __init__(self, cache: _LRU, key: str, present: bool = True):
        self._cache, self._key, self._present = cache, key, present

    def __getitem__(self, basin):
        return self._cache[basin][self._key]

    def __bool__(self):
        return self._present


class _Lookup:
    """`lookup_table[i] -> (basin, [end_index])` backed by per-basin int32 arrays."""

    def __init__(self, basins: list[str], valid: list[np.ndarray]):
        self.basins = basins
        self.valid = valid
        self.counts = np.array([len(v) for v in valid], dtype=np.int64)
        self.offsets = np.concatenate([[0], np.cumsum(self.counts)])

    def __len__(self):
        return int(self.offsets[-1])

    def locate(self, item: int) -> tuple[int, int]:
        b = int(np.searchsorted(self.offsets, item, side="right") - 1)
        return b, int(self.valid[b][item - self.offsets[b]])

    def __getitem__(self, item: int):
        b, idx = self.locate(int(item))
        return self.basins[b], [idx]


class _Stats:
    """Streaming mean/std (ddof=0) and min/max per feature, in float64."""

    def __init__(self):
        self.n: dict[str, float] = {}
        self.s: dict[str, float] = {}
        self.ss: dict[str, float] = {}
        self.lo: dict[str, float] = {}
        self.hi: dict[str, float] = {}

    def add(self, name: str, values: np.ndarray) -> None:
        v = values[~np.isnan(values)].astype(np.float64)
        if v.size == 0:
            self.n.setdefault(name, 0.0)
            return
        self.n[name] = self.n.get(name, 0.0) + v.size
        self.s[name] = self.s.get(name, 0.0) + v.sum()
        self.ss[name] = self.ss.get(name, 0.0) + (v * v).sum()
        self.lo[name] = min(self.lo.get(name, np.inf), v.min())
        self.hi[name] = max(self.hi.get(name, -np.inf), v.max())

    def mean(self, name: str) -> float:
        n = self.n.get(name, 0.0)
        return self.s[name] / n if n else np.nan

    def std(self, name: str) -> float:
        n = self.n.get(name, 0.0)
        if not n:
            return np.nan
        m = self.s[name] / n
        return float(np.sqrt(max(self.ss[name] / n - m * m, 0.0)))


class ZarrCubeDataset(BaseDataset):
    options: DatasetOptions = DatasetOptions()

    @classmethod
    def configure(cls, options: DatasetOptions) -> None:
        cls.options = options

    def __init__(self, cfg: Config, is_train: bool, period: str, basin: str = None, additional_features: list = [], id_to_int: dict = {}, scaler: dict = {}):
        opts = self.options
        paths = opts.cube or [str(cfg.data_dir)]
        self._cube = Cube(paths, CubeDims.from_dict(opts.dims), max_time=None if opts.allow_frozen_test else FROZEN_TEST_START)
        if additional_features or cfg.additional_feature_files:
            raise NotImplementedError("additional feature files are not supported by the streaming dataset")
        if cfg.train_data_file is not None or cfg.save_train_data:
            raise NotImplementedError("train_data_file/save_train_data are not supported by the streaming dataset")
        if cfg.use_frequencies and len(cfg.use_frequencies) > 1:
            raise NotImplementedError("multi-frequency runs are not supported by the streaming dataset")
        super().__init__(cfg=cfg, is_train=is_train, period=period, basin=basin, additional_features=[], id_to_int=id_to_int, scaler=scaler)

    # ------------------------------------------------------------------ hooks BaseDataset calls

    def _load_basin_data(self, basin: str) -> pd.DataFrame:
        start, end = self._load_window
        df = self._cube.load_dynamic(basin, self._cube_columns, start, end)
        for f in self._forecast_features:
            df[f] = np.float32(np.nan)
        return df

    def _load_attributes(self) -> pd.DataFrame:
        return self._cube.load_static(self.basins, self.cfg.static_attributes)

    # ------------------------------------------------------------------ loading

    def _plan_columns(self) -> None:
        cfg = self.cfg
        dyn = cfg.dynamic_inputs_flattened if isinstance(cfg.dynamic_inputs, list) else [i for v in cfg.dynamic_inputs.values() for i in v]
        wanted = sorted(set(cfg.target_variables + cfg.evolving_attributes + cfg.mass_inputs + cfg.autoregressive_inputs + dyn + cfg.dynamic_conceptual_inputs))
        derived = {f"{f}_shift{s}" for f, shifts in cfg.lagged_features.items() for s in (shifts if isinstance(shifts, list) else [shifts])}
        derived |= {f"{f}_copy{n}" for f, k in cfg.duplicate_features.items() for n in range(1, k + 1)}
        self._forecast_features = [c for c in wanted if c not in derived and self._cube.has(c) and self._cube.kind(c) == "forecast"]
        base = {c for c in wanted if c not in derived and c not in self._forecast_features}
        base |= set(cfg.lagged_features) | set(cfg.duplicate_features)
        missing_targets = [t for t in cfg.target_variables if not self._cube.has(t)]
        if missing_targets and self.is_train:
            raise KeyError(f"target variables {missing_targets} are not in the cube")
        self._cube_columns = sorted(c for c in base if self._cube.has(c))
        self._max_lag = max([max(s) if isinstance(s, list) else s for s in cfg.lagged_features.values()] or [0])

    def _period_bounds(self, basin: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        dates = self.start_and_end_dates[basin]
        warmup = (self.seq_len[0] - self._predict_last_n[0]) * pd.tseries.frequencies.to_offset(self.frequencies[0] if self.frequencies else "1h")
        start = min(dates["start_dates"]) - warmup - pd.Timedelta(hours=self._max_lag)
        end = max(dates["end_dates"]) + pd.Timedelta(days=1, hours=-1)
        return start, end

    def _basin_frame(self, basin: str) -> pd.DataFrame | None:
        """The basin's frame exactly as BaseDataset._load_or_create_xarray_dataset builds it (raw, unnormalized)."""
        self._load_window = self._period_bounds(basin)
        saved = self.basins, self._disable_pbar
        self.basins, self._disable_pbar = [basin], True
        try:
            xr = self._load_or_create_xarray_dataset()
        except (NoTrainDataError, NoEvaluationDataError):
            return None
        finally:
            self.basins, self._disable_pbar = saved
        df = xr.sel(basin=basin).to_dataframe()
        return df.drop(columns=[c for c in ("basin",) if c in df.columns])

    def _dynamic_cols(self) -> list[str]:
        cfg = self.cfg
        cols = cfg.mass_inputs + (cfg.dynamic_inputs_flattened if isinstance(cfg.dynamic_inputs, list) else cfg.dynamic_inputs[self.frequencies[0]])
        cols = cols + cfg.dynamic_conceptual_inputs
        return [c for c in cols if c not in self._forecast_features]

    def _load_data(self):
        self._plan_columns()
        self._load_combined_attributes()
        stats = _Stats() if self._compute_scaler else None
        fc_stats = _Stats()
        basins, valid, frames = [], [], {}
        for basin in tqdm(self.basins, file=sys.stdout, disable=self.cfg.verbose == 0 or not self.is_train, desc="Indexing basins"):
            df = self._basin_frame(basin)
            if df is None:
                continue
            if stats is not None:
                for col in df.columns:
                    stats.add(col, df[col].to_numpy())
            if self.cfg.loss.lower() in ["nse", "weightednse"]:
                obs = df[self.cfg.target_variables].to_numpy().T
                if np.sum(~np.isnan(obs)) > 1:
                    self._per_basin_target_stds[basin] = torch.tensor(np.expand_dims(np.nanstd(obs, axis=1), 0), dtype=torch.float32)
                else:
                    self._per_basin_target_stds[basin] = torch.full((1, obs.shape[0]), np.nan, dtype=torch.float32)
            flags = self._flags(df)
            idx = np.flatnonzero(flags == 1).astype(np.int32)
            if self._forecast_features and stats is not None:
                for _, _, fvals, names in self._cube.load_forecast(basin, self._forecast_features, df.index[0], df.index[-1]).values():
                    for j, f in enumerate(names):
                        fc_stats.add(f, fvals[..., j].ravel())
            if idx.size:
                basins.append(basin)
                valid.append(idx)
                if not self.is_train:
                    self.period_starts[basin] = pd.Timestamp(df.index[0])
            if len(self.basins) == 1:
                frames[basin] = df
        if stats is not None:
            self._set_scaler(stats, fc_stats)
        self.lookup_table = _Lookup(basins, valid)
        self.num_samples = len(self.lookup_table)
        if self.num_samples == 0:
            raise NoTrainDataError if self.is_train else NoEvaluationDataError
        capacity = self.options.cache_basins or 2 * self.options.block_basins
        self._blocks = _LRU(capacity, self._load_block)
        for basin, df in frames.items():
            self._blocks[basin] = self._block_from_frame(basin, df)
        self._x_d = _View(self._blocks, "x_d")
        self._y = _View(self._blocks, "y")
        self._dates = _View(self._blocks, "dates")
        self._x_s = _View(self._blocks, "x_s", present=bool(self.cfg.evolving_attributes))
        self._group_dropout = self._resolve_group_dropout()

    def _flags(self, df: pd.DataFrame) -> np.ndarray:
        cfg = self.cfg
        n = len(df)
        required = [c for c in self._dynamic_cols() if c not in self.options.optional_inputs]
        x_d = [df[required].to_numpy(np.float64)] if self.is_train else None
        x_s = [df[cfg.evolving_attributes].to_numpy(np.float64)] if self.is_train and cfg.evolving_attributes else None
        y = [df[cfg.target_variables].to_numpy(np.float64)] if self.is_train else None
        return _validate_samples(x_d=x_d, x_s=x_s, y=y, frequency_maps=[np.arange(n)], seq_length=self.seq_len, predict_last_n=self._predict_last_n)

    def _set_scaler(self, stats: _Stats, fc_stats: _Stats) -> None:
        names = list(stats.n)
        center = {k: np.float32(stats.mean(k)) for k in names}
        scale = {k: np.float32(stats.std(k)) for k in names}
        for f in self._forecast_features:
            center[f], scale[f] = np.float32(fc_stats.mean(f)), np.float32(fc_stats.std(f))
        for feature, spec in self.cfg.custom_normalization.items():
            for key, val in spec.items():
                val = "none" if val is None else str(val).lower()
                if key == "centering":
                    if val == "none":
                        center[feature] = np.float32(0.0)
                    elif val == "min":
                        center[feature] = np.float32(stats.lo[feature])
                    elif val == "median":
                        raise NotImplementedError("median centering needs a full pass; use mean, min or none")
                    elif val != "mean":
                        raise ValueError(f"Unknown centering method {val}")
                elif key == "scaling":
                    if val == "none":
                        scale[feature] = np.float32(1.0)
                    elif val == "minmax":
                        scale[feature] = np.float32(stats.hi[feature] - stats.lo[feature])
                    elif val != "std":
                        raise ValueError(f"Unknown scaling method {val}")
                else:
                    raise ValueError("Unknown dict key. Use 'centering' and/or 'scaling' for each feature.")
        self.scaler["xarray_feature_center"] = xarray.Dataset({k: ((), v) for k, v in center.items()})
        self.scaler["xarray_feature_scale"] = xarray.Dataset({k: ((), v) for k, v in scale.items()})

    def _scaler_value(self, key: str, name: str) -> float:
        return float(self.scaler[key][name].values)

    def _block_from_frame(self, basin: str, df: pd.DataFrame) -> dict:
        cfg = self.cfg
        freq = self.frequencies[0]
        norm = {}
        for col in df.columns:
            c, s = self._scaler_value("xarray_feature_center", col), self._scaler_value("xarray_feature_scale", col)
            norm[col] = ((df[col].to_numpy(np.float32) - np.float32(c)) / np.float32(s)).astype(np.float32)
        cols = self._dynamic_cols() + [c for c in cfg.autoregressive_inputs if c not in self._dynamic_cols()]
        block = {
            "x_d": {freq: {k: torch.from_numpy(norm[k][:, None]) for k in cols}},
            "y": {freq: torch.from_numpy(np.stack([norm[t] for t in cfg.target_variables], axis=1))},
            "dates": {freq: df.index.to_numpy()},
        }
        if cfg.evolving_attributes:
            block["x_s"] = {freq: torch.from_numpy(np.stack([norm[a] for a in cfg.evolving_attributes], axis=1))}
        if self._forecast_features:
            products = self._cube.load_forecast(basin, self._forecast_features, df.index[0] - pd.Timedelta(days=16), df.index[-1])
            for _, _, values, names in products.values():
                for j, f in enumerate(names):
                    c, s = self._scaler_value("xarray_feature_center", f), self._scaler_value("xarray_feature_scale", f)
                    values[..., j] = (values[..., j] - c) / s
            block["forecast"] = products
        return block

    def _load_block(self, basin: str) -> dict:
        df = self._basin_frame(basin)
        if df is None:
            raise KeyError(f"basin {basin} has no data in the {self.period} period")
        return self._block_from_frame(basin, df)

    # ------------------------------------------------------------------ samples

    def _resolve_group_dropout(self) -> list[tuple[list[str], float]]:
        groups = self.cfg.hindcast_inputs if self.cfg.hindcast_inputs else self.cfg.dynamic_inputs
        if groups and isinstance(groups[0], str):
            groups = [groups]
        out = []
        for key, p in self.options.group_dropout.items():
            members = next((g for g in groups if key in g), None) if not str(key).isdigit() else groups[int(key)]
            if members is None:
                raise KeyError(f"group_dropout key {key!r} matches no input group")
            out.append((list(members), float(p)))
        return out

    def _forecast_inputs(self, basin: str, idx: int, member: int | None) -> dict[str, torch.Tensor]:
        """Forecast-branch inputs from the latest init available at the sample's issue time, per product.

        Hour h of the forecast window takes the value at the smallest lead >= h (hour-ending convention), so
        3-hourly products fill the hours they cover; hours past a product's last lead stay NaN (masked).
        """
        dates = self._blocks[basin]["dates"][self.frequencies[0]]
        L = self.cfg.forecast_seq_length
        issue_time = pd.Timestamp(dates[idx - L])
        out = {}
        for product, (issues, leads, values, names) in self._blocks[basin]["forecast"].items():
            latency = pd.Timedelta(hours=self.options.forecast_latency_h.get(product, 0.0))
            pos = issues.searchsorted(issue_time - latency, side="right") - 1
            cols = np.full((L, len(names)), np.nan, dtype=np.float32)
            if pos >= 0:
                offset = (issue_time - issues[pos]) / pd.Timedelta(hours=1)
                wanted = offset + np.arange(1, L + 1)
                li = np.searchsorted(leads, wanted, side="left")
                spacing = np.diff(leads, prepend=0.0)
                ok = li < len(leads)
                ok[ok] &= leads[li[ok]] - wanted[ok] < spacing[li[ok]]
                m = member if member is not None else np.random.randint(values.shape[2])
                m = min(m, values.shape[2] - 1)
                cols[ok] = values[pos, li[ok], m, :]
            for j, f in enumerate(names):
                out[f] = torch.from_numpy(np.ascontiguousarray(cols[:, j : j + 1]))
        return out

    def __getitem__(self, item: int) -> dict:
        sample = super().__getitem__(item)
        basin, (idx,) = self.lookup_table[item]
        if self._forecast_features:
            key = "x_d_forecast" if self.cfg.forecast_inputs_flattened else "x_d"
            sample[key].update(self._forecast_inputs(basin, idx, None if self.is_train else getattr(self, "forecast_member", 0)))
        if self.is_train and self._group_dropout:
            key = "x_d_hindcast" if self.cfg.hindcast_inputs_flattened else "x_d"
            for members, p in self._group_dropout:
                if np.random.rand() < p:
                    for f in members:
                        if f in sample[key]:
                            sample[key][f] = torch.full_like(sample[key][f], float("nan"))
        return sample

    def sample_dates(self) -> list[tuple[str, int, pd.Timestamp]]:
        """(basin, end_index, date of the last hindcast step) for every sample, in lookup order (evaluation helper)."""
        L = self.cfg.forecast_seq_length or 0
        out = []
        for basin, idx in zip(self.lookup_table.basins, self.lookup_table.valid):
            dates = self._blocks[basin]["dates"][self.frequencies[0]]
            out.extend((basin, int(i), pd.Timestamp(dates[int(i) - L])) for i in idx)
        return out


class BasinBlockBatchSampler(Sampler[list[int]]):
    """Batches drawn from K basins at a time (plan §5.2), reshuffled every epoch.

    Leftover samples of one block are carried into the next block's first batch, so every batch but the last
    has `batch_size` samples.
    """

    def __init__(self, lookup: _Lookup, batch_size: int, block_basins: int = 16, seed: int | None = None):
        self.lookup = lookup
        self.batch_size = batch_size
        self.block_basins = max(1, block_basins)
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return -(-len(self.lookup) // self.batch_size)

    def __iter__(self):
        order = self.rng.permutation(len(self.lookup.basins))
        carry = np.empty(0, dtype=np.int64)
        for k in range(0, len(order), self.block_basins):
            block = order[k : k + self.block_basins]
            idx = np.concatenate([np.arange(self.lookup.offsets[b], self.lookup.offsets[b + 1]) for b in block])
            idx = np.concatenate([carry, self.rng.permutation(idx)])
            n_full = len(idx) // self.batch_size
            for i in range(n_full):
                yield idx[i * self.batch_size : (i + 1) * self.batch_size].tolist()
            carry = idx[n_full * self.batch_size :]
        if len(carry):
            yield carry.tolist()


register_dataset("flowcast_zarr", ZarrCubeDataset)
