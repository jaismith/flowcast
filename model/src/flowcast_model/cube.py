"""Training cube: the Zarr store(s) the streaming dataset reads (rebuild plan §5.2).

Supported layout (what `xarray.Dataset.to_zarr` writes):

| kind     | dims                                         | notes                                                   |
|----------|----------------------------------------------|---------------------------------------------------------|
| dynamic  | (basin, time)                                | hourly, UTC; float; chunked about 1 basin x 1+ year     |
| static   | (basin,)                                     | numeric basin attributes                                |
| forecast | (basin, issue_time, lead[, member])          | archived forecast forcing; `lead` in hours              |

A stacked layout, one variable with an extra feature dimension such as `dynamic(basin, time, feature)`, also
works: its features are addressed by the values of that dimension's coordinate. Dimension names are configurable
(`CubeDims`), so a cube written with e.g. `gauge_id`/`date` needs no rewrite. A dataset split across several
stores (e.g. train/validation and test written separately) is read as one: basins are unioned and time series
are concatenated in time.

Frozen test guard: by default nothing at or after `FROZEN_TEST_START` (WY2023) is ever read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

FROZEN_TEST_START = pd.Timestamp("2022-10-01T00:00")


class FrozenTestError(RuntimeError):
    """Raised when code asks for data inside the frozen test years."""


@dataclass(frozen=True)
class CubeDims:
    basin: str = "basin"
    time: str = "time"
    issue: str = "issue_time"
    lead: str = "lead"
    member: str = "member"
    feature: tuple[str, ...] = ("feature", "variable", "static_feature", "attribute", "forecast_feature")

    @classmethod
    def from_dict(cls, d: dict | None) -> "CubeDims":
        if not d:
            return cls()
        d = dict(d)
        if "feature" in d and isinstance(d["feature"], str):
            d["feature"] = (d["feature"],)
        elif "feature" in d:
            d["feature"] = tuple(d["feature"])
        return cls(**d)


@dataclass(frozen=True)
class FeatureRef:
    kind: str  # dynamic | static | forecast
    store: int
    var: str
    feature_dim: str | None = None
    feature_index: int | None = None


def _open(path: str | Path) -> xr.Dataset:
    path = str(path)
    if path.endswith(".nc"):
        return xr.open_dataset(path)
    return xr.open_zarr(path, chunks=None, consolidated=None)


@dataclass
class Cube:
    paths: Sequence[str | Path]
    dims: CubeDims = field(default_factory=CubeDims)
    max_time: pd.Timestamp | None = FROZEN_TEST_START

    def __post_init__(self):
        if isinstance(self.paths, (str, Path)):
            self.paths = [self.paths]
        self.stores = [_open(p) for p in self.paths]
        self._basin_pos: list[dict[str, int]] = []
        for ds in self.stores:
            ids = [str(b) for b in ds[self.dims.basin].values]
            self._basin_pos.append({b: i for i, b in enumerate(ids)})
        self._times = [pd.DatetimeIndex(ds[self.dims.time].values) if self.dims.time in ds.coords else None for ds in self.stores]
        self._refs = self._index_features()

    # ------------------------------------------------------------------ discovery

    def _kind(self, dims: tuple[str, ...]) -> str | None:
        d = self.dims
        rest = [x for x in dims if x not in (d.basin,)]
        if d.basin not in dims:
            return None
        if d.issue in dims and d.lead in dims:
            return "forecast"
        if d.time in dims:
            return "dynamic"
        if not rest or (len(rest) == 1 and rest[0] in d.feature):
            return "static"
        return None

    def _index_features(self) -> dict[str, list[FeatureRef]]:
        refs: dict[str, list[FeatureRef]] = {}
        for s, ds in enumerate(self.stores):
            for name, var in ds.data_vars.items():
                kind = self._kind(var.dims)
                if kind is None:
                    continue
                fdims = [x for x in var.dims if x in self.dims.feature]
                if fdims:
                    fdim = fdims[0]
                    for i, f in enumerate(ds[fdim].values):
                        refs.setdefault(str(f), []).append(FeatureRef(kind, s, name, fdim, i))
                else:
                    refs.setdefault(str(name), []).append(FeatureRef(kind, s, name))
        return refs

    @property
    def basins(self) -> list[str]:
        seen: dict[str, None] = {}
        for pos in self._basin_pos:
            seen.update(dict.fromkeys(pos))
        return list(seen)

    def features(self, kind: str | None = None) -> list[str]:
        return [f for f, rs in self._refs.items() if kind is None or rs[0].kind == kind]

    def kind(self, feature: str) -> str:
        if feature not in self._refs:
            raise KeyError(f"feature {feature!r} is not in the cube; available: {sorted(self._refs)}")
        return self._refs[feature][0].kind

    def has(self, feature: str) -> bool:
        return feature in self._refs

    def attrs(self) -> dict:
        out: dict = {}
        for ds in self.stores:
            out.update(ds.attrs)
        return out

    # ------------------------------------------------------------------ reads

    def _check_time(self, end: pd.Timestamp) -> None:
        if self.max_time is not None and end >= self.max_time:
            raise FrozenTestError(f"request reaches {end}, inside the frozen test period (>= {self.max_time}); refusing to read it")

    def _read(self, ref: FeatureRef, basin_pos: int, **isel) -> np.ndarray:
        var = self.stores[ref.store][ref.var]
        sel = {self.dims.basin: basin_pos, **isel}
        if ref.feature_dim is not None:
            sel[ref.feature_dim] = ref.feature_index
        return np.asarray(var.isel(sel).values)

    def load_dynamic(self, basin: str, features: Iterable[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Hourly frame on the full [start, end] index; NaN where no store has data."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        self._check_time(end)
        index = pd.date_range(start, end, freq="h", name="date")
        out = pd.DataFrame(index=index, dtype=np.float32)
        for f in features:
            col = np.full(len(index), np.nan, dtype=np.float32)
            filled = np.zeros(len(index), dtype=bool)
            for ref in self._refs.get(f, []):
                if ref.kind != "dynamic":
                    raise ValueError(f"feature {f!r} is {ref.kind}, not dynamic")
                pos = self._basin_pos[ref.store].get(basin)
                times = self._times[ref.store]
                if pos is None or times is None:
                    continue
                lo, hi = times.searchsorted(start, "left"), times.searchsorted(end, "right")
                if hi <= lo:
                    continue
                values = self._read(ref, pos, **{self.dims.time: slice(lo, hi)}).astype(np.float32)
                target = index.get_indexer(times[lo:hi])
                ok = (target >= 0) & ~filled[np.clip(target, 0, None)]
                col[target[ok]] = values[ok]
                filled[target[ok]] |= ~np.isnan(values[ok])
            if f not in self._refs:
                raise KeyError(f"feature {f!r} is not in the cube")
            out[f] = col
        return out

    def load_static(self, basins: Sequence[str], features: Iterable[str]) -> pd.DataFrame:
        features = list(features)
        df = pd.DataFrame(index=pd.Index(list(basins), name="basin"), columns=features, dtype=np.float64)
        for f in features:
            for ref in self._refs.get(f, []):
                if ref.kind != "static":
                    raise ValueError(f"feature {f!r} is {ref.kind}, not static")
                ds = self.stores[ref.store]
                var = ds[ref.var]
                if ref.feature_dim is not None:
                    var = var.isel({ref.feature_dim: ref.feature_index})
                values = pd.Series(np.asarray(var.values, dtype=np.float64), index=[str(b) for b in ds[self.dims.basin].values])
                missing = df[f].isna()
                df.loc[missing, f] = values.reindex(df.index[missing]).to_numpy()
            if f not in self._refs:
                raise KeyError(f"static attribute {f!r} is not in the cube")
        return df

    def load_forecast(self, basin: str, features: Sequence[str], issue_start: pd.Timestamp, issue_end: pd.Timestamp) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]:
        """(issue_times, leads_h, values[issue, lead, member, feature]) for issues in [issue_start, issue_end]."""
        issue_start, issue_end = pd.Timestamp(issue_start), pd.Timestamp(issue_end)
        refs = [self._refs[f][0] for f in features]
        if any(r.kind != "forecast" for r in refs):
            raise ValueError("load_forecast only reads forecast features")
        stores = {r.store for r in refs}
        if len(stores) != 1:
            raise ValueError("forecast features must live in one store")
        ds = self.stores[stores.pop()]
        issues = pd.DatetimeIndex(ds[self.dims.issue].values)
        leads = np.asarray(ds[self.dims.lead].values)
        if np.issubdtype(leads.dtype, np.timedelta64):
            leads = leads / np.timedelta64(1, "h")
        leads = leads.astype(float)
        lo, hi = issues.searchsorted(issue_start, "left"), issues.searchsorted(issue_end, "right")
        pos = self._basin_pos[refs[0].store].get(basin)
        n_member = ds.sizes.get(self.dims.member, 1)
        values = np.full((max(hi - lo, 0), len(leads), n_member, len(features)), np.nan, dtype=np.float32)
        if pos is not None and hi > lo:
            max_valid = issues[hi - 1] + pd.Timedelta(hours=float(leads.max()))
            self._check_time(max_valid)
            for j, ref in enumerate(refs):
                arr = self._read(ref, pos, **{self.dims.issue: slice(lo, hi)})
                dims = [d for d in ds[ref.var].dims if d not in (self.dims.basin, ref.feature_dim)]
                if self.dims.member not in dims:
                    arr = arr[..., None]
                    dims = [*dims, self.dims.member]
                arr = np.transpose(arr, [dims.index(self.dims.issue), dims.index(self.dims.lead), dims.index(self.dims.member)])
                values[..., j] = arr
        return issues[lo:hi], leads, values


def write_cube(
    path: str | Path,
    dynamic: dict[str, pd.DataFrame],
    static: pd.DataFrame,
    attrs: dict | None = None,
    time_chunk: int = 8760 * 4,
) -> None:
    """Write a per-variable cube. `dynamic` maps basin -> hourly frame (columns = features)."""
    basins = list(dynamic)
    index = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in dynamic.values()])))
    index = pd.date_range(index.min(), index.max(), freq="h")
    features = sorted(set().union(*[set(df.columns) for df in dynamic.values()]))
    data_vars = {}
    for f in features:
        arr = np.full((len(basins), len(index)), np.nan, dtype=np.float32)
        for i, b in enumerate(basins):
            if f in dynamic[b]:
                arr[i] = dynamic[b][f].reindex(index).to_numpy(np.float32)
        data_vars[f] = (("basin", "time"), arr)
    static = static.reindex(basins)
    for col in static.columns:
        data_vars[col] = (("basin",), static[col].to_numpy(np.float64))
    ds = xr.Dataset(data_vars, coords={"basin": np.array(basins, dtype=str), "time": index.values}, attrs=attrs or {})
    encoding = {f: {"chunks": (1, min(time_chunk, len(index)))} for f in features}
    ds.to_zarr(str(path), mode="w", encoding=encoding, consolidated=True)
