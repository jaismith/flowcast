"""Streaming Zarr dataset for NeuralHydrology (rebuild plan §5.2, milestone 1.4).

`ZarrCubeDataset` is registered as NeuralHydrology dataset `flowcast_zarr`. It keeps NeuralHydrology's own
per-basin preprocessing (period slicing, warmup, lagged/duplicated features, sample validation) and its
`__getitem__` slicing, so a sample is built exactly as the stock loader builds it. What changes:

* Basins are read one at a time from the cube instead of all at once into one in-memory xarray.
* Normalization statistics and per-basin target stds are accumulated in one streaming pass.
* The sample index is one int32 array of valid end positions per basin, not a Python dict of tuples. It and the
  statistics are cached in the run directory (`index_cache.py`), so a resumed Spot run skips the indexing pass.
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
import time
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

from . import index_cache
from .cube import Cube, CubeDims, FROZEN_TEST_START

LOGGER = logging.getLogger(__name__)
INDEX_LOG_INTERVAL_S = 60
# Options of dropped experiments with their unset values. Runs trained before their removal (including the shipped
# models) record them, unset, in flowcast.yml; those still load, and a run that set one fails loudly.
RETIRED_OPTIONS = {"fill_absent": {}, "mixed_forcing": {}, "mixed_forcing_p": 0.0, "forecast_qmap": {}, "loss_weight": None}


@dataclass
class DatasetOptions:
    """Flowcast-specific dataset options (the `flowcast.dataset` block of a training config)."""

    cube: list[str] = field(default_factory=list)
    dims: dict = field(default_factory=dict)
    optional_inputs: list[str] = field(default_factory=list)
    group_dropout: dict[str, float] = field(default_factory=dict)
    # The same for forecast-branch input groups, e.g. observed future flow fed as a stand-in for a flow forecast.
    forecast_group_dropout: dict[str, float] = field(default_factory=dict)
    block_basins: int = 64
    # Samples per basin chunk in a training block (BasinBlockBatchSampler); None = whole basins per block, which
    # with max_updates_per_epoch far below a block's size trains each epoch on block_basins basins only.
    chunk_samples: int | None = 2048
    # Basins cached per data-loader process (0 = sized from the block). Each worker has its own cache and a full-cube
    # basin takes about 35 MB, so 3 workers caching 2 x 64 basins ran a 16 GB instance out of memory.
    cache_basins: int = 0
    # Keep cached forecast arrays (normalized) in float16: archived ensembles (e.g. 20 years of GEFS reforecast) are
    # most of a basin's cached size. Samples are still float32.
    forecast_float16: bool = False
    # Arrays each process reads concurrently when it loads a basin. At a block swap every loader worker loads the
    # block's basins at once, so this pays off with spare cores (e.g. 8 vCPUs for 3 workers).
    read_threads: int = 1
    forecast_latency_h: dict[str, float] = field(default_factory=dict)
    allow_frozen_test: bool = False
    # Evaluation only (ignored in training): hindcast-branch inputs forced missing (products not available in
    # real time), and forecast-branch inputs replaced by an archived forecast product ("perfect prog").
    # Forecast-branch inputs holding a hindcast input's last value (at the issue time) over the whole forecast
    # window, e.g. persistence of observed flow or of gauged dam outflow (plan §3). Missing if the source is.
    persist_inputs: dict[str, str] = field(default_factory=dict)
    # Extra forecast products filling the same model input where it has no data, e.g. the GEFSv12 reforecast
    # (2000-2019) for operational GEFS (2020-10 on): {source feature: input feature}. The input's normalization
    # comes from the sources' training-period values.
    forecast_aliases: dict[str, str] = field(default_factory=dict)
    # Fill values for static attributes that are missing by design (e.g. per-slot upstream-gauge attributes of an
    # empty slot), so they can be model inputs: {attribute: value}.
    static_fill: dict[str, float] = field(default_factory=dict)
    mask_hindcast: list[str] = field(default_factory=list)
    mask_forecast: list[str] = field(default_factory=list)
    substitute_forecast: dict[str, str] = field(default_factory=dict)
    # Evaluation only: hindcast-branch inputs carried forward over gaps of up to N hours, {feature: N}. Gauges under
    # ice often report every 2-12 h; a missing latest lagged flow zeroes the residual anchor (models.py), which is
    # the all-basin mean flow, and training (whole-window group dropout) never shows a window missing only its end.
    ffill_hindcast_h: dict[str, int] = field(default_factory=dict)
    forecast_member: int | None = None
    # Training only: per-step loss weights toward high flows and rising limbs (models.weight_cmal_loss),
    # {high_quantile, high, rise_quantile, rise}. A step weighs 1 + high * [target above the basin's high_quantile]
    # + rise * [target rising and above the basin's rise_quantile], with quantiles of the basin's training-period
    # target; the loss rescales the weights to mean 1 over each batch's scored steps.
    flow_weight: dict[str, float] = field(default_factory=dict)
    # Training only: flood windows drawn more often, {quantile, factor}. A sample whose forecast window holds a target
    # above the basin's training-period `quantile` is drawn `factor` times per pass over the data instead of once.
    flood_oversample: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "DatasetOptions":
        d = dict(d or {})
        for key, default in RETIRED_OPTIONS.items():
            if d.pop(key, None) not in (None, default):
                raise ValueError(f"dataset option {key!r} belonged to a dropped experiment and was removed (docs/decisions.md)")
        if isinstance(d.get("cube"), str):
            d["cube"] = [d["cube"]]
        return cls(**d)


def forward_fill(x: torch.Tensor, limit: int) -> torch.Tensor:
    """Carry the last non-missing value forward along the first (time) axis, at most `limit` steps past it."""
    if limit <= 0:
        return x
    steps = torch.arange(x.shape[0], device=x.device).view(-1, *([1] * (x.dim() - 1))).expand_as(x)
    last = torch.where(torch.isnan(x), torch.full_like(steps, -1), steps).cummax(dim=0).values
    fill = x.gather(0, last.clamp(min=0))
    use = torch.isnan(x) & (last >= 0) & (steps - last <= limit)
    return torch.where(use, fill, x)


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
        self._cube = Cube(paths, CubeDims.from_dict(opts.dims), max_time=None if opts.allow_frozen_test else FROZEN_TEST_START, read_threads=opts.read_threads)
        if additional_features or cfg.additional_feature_files:
            raise NotImplementedError("additional feature files are not supported by the streaming dataset")
        if cfg.train_data_file is not None or cfg.save_train_data:
            raise NotImplementedError("train_data_file/save_train_data are not supported by the streaming dataset")
        if cfg.use_frequencies and len(cfg.use_frequencies) > 1:
            raise NotImplementedError("multi-frequency runs are not supported by the streaming dataset")
        # NeuralHydrology's `scaler={}` default is shared and gets mutated; never reuse another dataset's scaler
        scaler = scaler if scaler else {}
        super().__init__(cfg=cfg, is_train=is_train, period=period, basin=basin, additional_features=[], id_to_int=id_to_int, scaler=scaler)

    # ------------------------------------------------------------------ hooks BaseDataset calls

    def _load_basin_data(self, basin: str) -> pd.DataFrame:
        start, end = self._load_window
        df = self._cube.load_dynamic(basin, self._cube_columns, start, end)
        for f in [*self._forecast_features, *self._persist]:
            df[f] = np.float32(np.nan)
        return df

    def _load_attributes(self) -> pd.DataFrame:
        df = self._cube.load_static(self.basins, self.cfg.static_attributes)
        fill = {k: v for k, v in self.options.static_fill.items() if k in df.columns}
        return df.fillna(fill) if fill else df

    # ------------------------------------------------------------------ loading

    def _plan_columns(self) -> None:
        cfg = self.cfg
        dyn = cfg.dynamic_inputs_flattened if isinstance(cfg.dynamic_inputs, list) else [i for v in cfg.dynamic_inputs.values() for i in v]
        wanted = sorted(set(cfg.target_variables + cfg.evolving_attributes + cfg.mass_inputs + cfg.autoregressive_inputs + dyn + cfg.dynamic_conceptual_inputs))
        derived = {f"{f}_shift{s}" for f, shifts in cfg.lagged_features.items() for s in (shifts if isinstance(shifts, list) else [shifts])}
        derived |= {f"{f}_copy{n}" for f, k in cfg.duplicate_features.items() for n in range(1, k + 1)}
        self._persist = {k: v for k, v in self.options.persist_inputs.items() if k in wanted}
        self._forecast_features = [c for c in wanted if c not in derived and c not in self._persist and self._cube.has(c) and self._cube.kind(c) == "forecast"]
        self._substitute = {} if self.is_train else dict(self.options.substitute_forecast)
        self._aliases = {src: dst for src, dst in self.options.forecast_aliases.items() if dst in self._forecast_features and self._cube.has(src)}
        self._norm_as = {src: dst for dst, src in self._substitute.items()} | self._aliases
        extra = [src for src in [*self._substitute.values(), *self._aliases] if src not in self._forecast_features]
        self._forecast_sources = self._forecast_features + list(dict.fromkeys(extra))
        self._input_sources = set(self._forecast_features) | set(self._aliases)
        self._substitute_sources = set(self._substitute.values())
        self._lead_index: dict[tuple[str, float], tuple[np.ndarray, np.ndarray]] = {}
        base = {c for c in wanted if c not in derived and c not in self._forecast_features and c not in self._persist}
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
        key = index_cache.cache_key(self)
        cached = index_cache.read(self, key) if key else None
        if cached is not None:
            basins, valid, extras = cached
            self._apply_cached(extras)
            frames = {}
            LOGGER.info("loaded the sample index of %d basins from the index cache", len(basins))
        else:
            basins, valid, frames = self._build_index()
            if key:
                index_cache.write(self, key, basins, valid)
        self.lookup_table = _Lookup(basins, valid)
        self.num_samples = len(self.lookup_table)
        if self.num_samples == 0:
            raise NoTrainDataError if self.is_train else NoEvaluationDataError
        # A chunked block touches at most block_basins basins; whole-basin blocks also keep the next block's.
        blocks = self.options.block_basins
        capacity = self.options.cache_basins or (blocks + 8 if self.options.chunk_samples else 2 * blocks)
        self._blocks = _LRU(capacity, self._load_block)
        for basin, df in frames.items():
            self._blocks[basin] = self._block_from_frame(basin, df)
        self._x_d = _View(self._blocks, "x_d")
        self._y = _View(self._blocks, "y")
        self._dates = _View(self._blocks, "dates")
        self._x_s = _View(self._blocks, "x_s", present=bool(self.cfg.evolving_attributes))
        self._group_dropout = self._resolve_group_dropout(self.options.group_dropout, self.cfg.hindcast_inputs or self.cfg.dynamic_inputs)
        self._forecast_group_dropout = self._resolve_group_dropout(self.options.forecast_group_dropout, self.cfg.forecast_inputs)

    def _apply_cached(self, extras: dict) -> None:
        state = extras["nh_state"]
        self.frequencies, self.seq_len, self._predict_last_n = state["frequencies"], state["seq_len"], state["predict_last_n"]
        if self._compute_scaler:
            names, center, scale = extras["scaler"]
            self.scaler["xarray_feature_center"] = xarray.Dataset({k: ((), np.float32(v)) for k, v in zip(names, center)})
            self.scaler["xarray_feature_scale"] = xarray.Dataset({k: ((), np.float32(v)) for k, v in zip(names, scale)})
        self._per_basin_target_stds.update(extras.get("stds", {}))
        self.period_starts.update(extras.get("period_starts", {}))

    def _build_index(self) -> tuple[list[str], list[np.ndarray], dict[str, pd.DataFrame]]:
        """One pass over the basins: valid sample end positions, statistics (training) and per-basin extras."""
        stats = _Stats() if self._compute_scaler else None
        fc_stats = _Stats()
        basins, valid, frames = [], [], {}
        last_log = time.monotonic()
        for i, basin in enumerate(tqdm(self.basins, file=sys.stdout, disable=self.cfg.verbose == 0 or not self.is_train, desc="Indexing basins")):
            # Also a liveness signal: the Spot job's guard treats a silent log as a stalled run.
            if time.monotonic() - last_log >= INDEX_LOG_INTERVAL_S:
                LOGGER.info("indexed %d of %d basins", i, len(self.basins))
                last_log = time.monotonic()
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
                stat_sources = self._forecast_features + list(self._aliases)
                for _, _, fvals, names in self._cube.load_forecast(basin, stat_sources, df.index[0], df.index[-1]).values():
                    for j, f in enumerate(names):
                        fc_stats.add(self._aliases.get(f, f), fvals[..., j].ravel())
            if idx.size:
                basins.append(basin)
                valid.append(idx)
                if not self.is_train:
                    self.period_starts[basin] = pd.Timestamp(df.index[0])
            if len(self.basins) == 1:
                frames[basin] = df
        if stats is not None:
            self._set_scaler(stats, fc_stats)
        return basins, valid, frames

    def _flags(self, df: pd.DataFrame) -> np.ndarray:
        cfg = self.cfg
        n = len(df)
        required = [c for c in self._dynamic_cols() if c not in self.options.optional_inputs and c not in self._persist]
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
        for f, src in self._persist.items():
            center[f], scale[f] = center[src], scale[src]
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
        if self.is_train and self.options.flow_weight:
            y = norm[cfg.target_variables[0]]
            fw = self.options.flow_weight
            block["flow_weight_thresholds"] = tuple(float(np.nanquantile(y, fw.get(k, 1.0))) if np.isfinite(y).any() else np.inf for k in ("high_quantile", "rise_quantile"))
        if self._forecast_sources:
            block["forecast"] = self._cube.load_forecast(
                basin,
                self._forecast_sources,
                df.index[0] - pd.Timedelta(days=16),
                df.index[-1],
                transform=lambda f, leads, x: self._normalize_forecast(f, x),
                dtype=np.float16 if self.options.forecast_float16 else np.float32,
            )
        return block

    def _normalize_forecast(self, feature: str, x: np.ndarray) -> np.ndarray:
        ref = self._norm_as.get(feature, feature)
        c, s = self._scaler_value("xarray_feature_center", ref), self._scaler_value("xarray_feature_scale", ref)
        return (x - c) / s

    def _load_block(self, basin: str) -> dict:
        df = self._basin_frame(basin)
        if df is None:
            raise KeyError(f"basin {basin} has no data in the {self.period} period")
        return self._block_from_frame(basin, df)

    # ------------------------------------------------------------------ samples

    @staticmethod
    def _resolve_group_dropout(spec: dict[str, float], groups) -> list[tuple[list[str], float]]:
        groups = list(groups or [])
        if groups and isinstance(groups[0], str):
            groups = [groups]
        out = []
        for key, p in spec.items():
            members = next((g for g in groups if key in g), None) if not str(key).isdigit() else groups[int(key)]
            if members is None:
                raise KeyError(f"group_dropout key {key!r} matches no input group")
            out.append((list(members), float(p)))
        return out

    def _forecast_picks(self, basin: str, idx: int, member: int | None) -> dict[str, tuple[int, np.ndarray, np.ndarray, int] | None]:
        """Per product: the latest init available at the sample's issue time and the lead and member it takes
        (None without an init). Random members are drawn here for every product, so the sample's random stream
        doesn't depend on which products it ends up reading.

        Hour h of the forecast window takes the value at the smallest lead >= h (hour-ending convention), so
        3-hourly products fill the hours they cover; hours past a product's last lead stay NaN (masked).
        """
        dates = self._blocks[basin]["dates"][self.frequencies[0]]
        L = self.cfg.forecast_seq_length
        issue_time = pd.Timestamp(dates[idx - L])
        picks = {}
        for product, (issues, leads, values, _) in self._blocks[basin]["forecast"].items():
            latency = pd.Timedelta(hours=self.options.forecast_latency_h.get(product, 0.0))
            pos = issues.searchsorted(issue_time - latency, side="right") - 1
            if pos < 0:
                picks[product] = None
                continue
            offset = (issue_time - issues[pos]) / pd.Timedelta(hours=1)
            key = (product, offset)
            if key not in self._lead_index:
                wanted = offset + np.arange(1, L + 1)
                li = np.searchsorted(leads, wanted, side="left")
                spacing = np.diff(leads, prepend=0.0)
                ok = li < len(leads)
                ok[ok] &= leads[li[ok]] - wanted[ok] < spacing[li[ok]]
                self._lead_index[key] = (li[ok], ok)
            m = member if member is not None else np.random.randint(values.shape[2])
            picks[product] = (pos, *self._lead_index[key], min(m, values.shape[2] - 1))
        return picks

    def _forecast_values(self, basin: str, product: str, pick: tuple[int, np.ndarray, np.ndarray, int] | None) -> dict[str, torch.Tensor]:
        _, _, values, names = self._blocks[basin]["forecast"][product]
        cols = np.full((self.cfg.forecast_seq_length, len(names)), np.nan, dtype=np.float32)
        if pick is not None:
            pos, li, ok, m = pick
            cols[ok] = values[pos, li, m, :]
        return {f: torch.from_numpy(np.ascontiguousarray(cols[:, j : j + 1])) for j, f in enumerate(names)}

    def __getitem__(self, item: int) -> dict:
        sample = super().__getitem__(item)
        b, idx = self.lookup_table.locate(int(item))
        basin = self.lookup_table.basins[b]
        if self.is_train:
            sample["basin_index"] = torch.tensor(b)
            if self.options.flow_weight:
                sample["flow_weight"] = self._flow_weight(sample["y"], self._blocks[basin]["flow_weight_thresholds"])
        if self._forecast_sources:
            key = "x_d_forecast" if self.cfg.forecast_inputs_flattened else "x_d"
            member = None if self.is_train else (self.options.forecast_member or 0)
            picks = self._forecast_picks(basin, idx, member)
            products = self._blocks[basin]["forecast"]
            values: dict[str, torch.Tensor] = {}
            for product, pick in picks.items():
                if self._input_sources.intersection(products[product][3]):
                    values.update(self._forecast_values(basin, product, pick))
            sample[key].update({f: v for f, v in values.items() if f in self._forecast_features})
            for src, dst in self._aliases.items():
                sample[key][dst] = torch.where(torch.isnan(sample[key][dst]), values[src], sample[key][dst])
            # substituted products (evaluation only) are read only when no model input comes from them
            if self._substitute:
                for product, pick in picks.items():
                    names = products[product][3]
                    if not self._input_sources.intersection(names) and self._substitute_sources.intersection(names):
                        values.update(self._forecast_values(basin, product, pick))
                for dst, src in self._substitute.items():
                    sample[key][dst] = values[src]
        if not self.is_train and self.options.ffill_hindcast_h:
            key = "x_d_hindcast" if self.cfg.hindcast_inputs_flattened else "x_d"
            for f, limit in self.options.ffill_hindcast_h.items():
                if f in sample[key]:
                    sample[key][f] = forward_fill(sample[key][f], int(limit))
        if not self.is_train and self.options.mask_hindcast:
            key = "x_d_hindcast" if self.cfg.hindcast_inputs_flattened else "x_d"
            for f in self.options.mask_hindcast:
                if f in sample[key]:
                    sample[key][f] = torch.full_like(sample[key][f], float("nan"))
        if not self.is_train and self.options.mask_forecast:
            key = "x_d_forecast" if self.cfg.forecast_inputs_flattened else "x_d"
            for f in self.options.mask_forecast:
                if f in sample[key]:
                    sample[key][f] = torch.full_like(sample[key][f], float("nan"))
        if self.is_train and (self._group_dropout or self._forecast_group_dropout):
            hkey = "x_d_hindcast" if self.cfg.hindcast_inputs_flattened else "x_d"
            fkey = "x_d_forecast" if self.cfg.forecast_inputs_flattened else "x_d"
            for key, dropout in ((hkey, self._group_dropout), (fkey, self._forecast_group_dropout)):
                for members, p in dropout:
                    if np.random.rand() < p:
                        for f in members:
                            if f in sample[key]:
                                sample[key][f] = torch.full_like(sample[key][f], float("nan"))
        if self._persist:
            hkey = "x_d_hindcast" if self.cfg.hindcast_inputs_flattened else "x_d"
            fkey = "x_d_forecast" if self.cfg.forecast_inputs_flattened else "x_d"
            for f, src in self._persist.items():
                last = sample[hkey][src][-1:]
                sample[fkey][f] = last.expand(sample[fkey][f].shape[0], -1).clone()
        return sample

    def _flow_weight(self, y: torch.Tensor, thresholds: tuple[float, float]) -> torch.Tensor:
        """Per-step loss weight [seq, 1] of a sample's (normalized) target; missing steps weigh 1."""
        high_thr, rise_thr = thresholds
        target = y[:, :1]
        rising = torch.zeros_like(target, dtype=torch.bool)
        rising[1:] = target[1:] > target[:-1]
        fw = self.options.flow_weight
        return 1.0 + fw.get("high", 0.0) * (target > high_thr).float() + fw.get("rise", 0.0) * (rising & (target > rise_thr)).float()

    def flood_repeats(self) -> list[np.ndarray]:
        """Per basin (lookup order), the local sample positions to add for `flood_oversample`: each sample whose
        forecast window holds a target above the basin's quantile, listed factor - 1 times."""
        spec = self.options.flood_oversample
        L = self._predict_last_n[0]
        out = []
        for basin, valid in zip(self.lookup_table.basins, self.lookup_table.valid):
            y = self._basin_frame(basin)[self.cfg.target_variables[0]].to_numpy(np.float64)
            high = np.nan_to_num(y, nan=-np.inf) > np.nanquantile(y, spec["quantile"])
            cum = np.concatenate([[0], np.cumsum(high)])
            hit = cum[valid + 1] - cum[np.maximum(valid + 1 - L, 0)] > 0
            out.append(np.repeat(np.flatnonzero(hit), int(spec["factor"]) - 1))
        return out

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
    has `batch_size` samples. After `set_epoch`, the order depends only on (seed, epoch), so a run resumed from
    a checkpoint continues the sequence instead of replaying the first epochs' order.

    With `chunk_samples`, a block is `block_basins` chunks of up to `chunk_samples` random samples of one basin,
    drawn from all basins' chunks in random order, instead of `block_basins` whole basins. A whole basin is about
    150k samples (hundreds of batches), so whole-basin blocks make an epoch capped by `max_updates_per_epoch` see
    only the first block's basins; chunked blocks keep consecutive batches mixing many basins.
    """

    def __init__(self, lookup: _Lookup, batch_size: int, block_basins: int = 16, seed: int | None = None, chunk_samples: int | None = None, repeats: list[np.ndarray] | None = None):
        self.lookup = lookup
        self.batch_size = batch_size
        self.block_basins = max(1, block_basins)
        self.chunk_samples = chunk_samples
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        # per basin, extra copies of some of its samples (local positions; ZarrCubeDataset.flood_repeats)
        self.repeats = repeats
        self.sizes = lookup.counts + (np.array([len(r) for r in repeats], dtype=np.int64) if repeats is not None else 0)

    def __len__(self) -> int:
        return -(-int(self.sizes.sum()) // self.batch_size)

    def _pool(self, b: int) -> np.ndarray:
        own = np.arange(self.lookup.counts[b])
        return own if self.repeats is None else np.concatenate([own, self.repeats[b]])

    def set_epoch(self, epoch: int) -> None:
        self.rng = np.random.default_rng(None if self.seed is None else [self.seed, epoch])

    def _blocks(self):
        offsets = self.lookup.offsets
        if not self.chunk_samples:
            for block in np.array_split(self.rng.permutation(len(self.lookup.basins)), range(self.block_basins, len(self.lookup.basins), self.block_basins)):
                yield np.concatenate([offsets[b] + self._pool(b) for b in block])
            return
        n_chunks = -(-self.sizes // self.chunk_samples)
        units = np.repeat(np.arange(len(n_chunks)), n_chunks)
        parts = np.concatenate([np.arange(n) for n in n_chunks])
        order = self.rng.permutation(len(units))
        base = int(self.rng.integers(2**62))
        for k in range(0, len(order), self.block_basins):
            chosen = order[k : k + self.block_basins]
            idx = []
            for b, part in zip(units[chosen], parts[chosen]):
                perm = np.random.default_rng([base, int(b)]).permutation(int(self.sizes[b]))
                if self.repeats is not None:
                    perm = self._pool(b)[perm]
                idx.append(offsets[b] + perm[part * self.chunk_samples : (part + 1) * self.chunk_samples])
            yield np.concatenate(idx)

    def __iter__(self):
        carry = np.empty(0, dtype=np.int64)
        for idx in self._blocks():
            idx = np.concatenate([carry, self.rng.permutation(idx)])
            n_full = len(idx) // self.batch_size
            for i in range(n_full):
                yield idx[i * self.batch_size : (i + 1) * self.batch_size].tolist()
            carry = idx[n_full * self.batch_size :]
        if len(carry):
            yield carry.tolist()


register_dataset("flowcast_zarr", ZarrCubeDataset)
