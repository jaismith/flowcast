"""Training cube: the Zarr store(s) the streaming dataset reads (rebuild plan §5.2).

Supported layout (what `xarray.Dataset.to_zarr` writes):

| kind     | dims                                         | notes                                                   |
|----------|----------------------------------------------|---------------------------------------------------------|
| dynamic  | (basin, time)                                | hourly, UTC; float; chunked about 1 basin x 1+ year     |
| static   | (basin,)                                     | numeric basin attributes                                |
| forecast | (basin, <p>_init, [<p>_member,] <p>_lead)    | archived forecast forcing per product `p`; lead in hours|

A stacked layout, one variable with an extra feature dimension such as `dynamic(basin, time, feature)` or
`static_all(basin, attribute)`, also works: its features are addressed by that dimension's coordinate values.
Any other extra dimension (e.g. elevation `band`, `month`) is expanded into one feature per index, named
`<var>_<dim><coord>` (e.g. `aorc_band_precip_mm_h_band2`). Forecast products are recognized by their
`<p>_init` / `<p>_lead` / optional `<p>_member` dimensions and returned per product, since each has its own
init times, lead grid and ensemble size. Dimension names are configurable
(`CubeDims`), so a cube written with e.g. `gauge_id`/`date` needs no rewrite. A dataset split across several
stores (e.g. train/validation and test written separately) is read as one: basins are unioned and time series
are concatenated in time.

Frozen test guard: by default nothing at or after `FROZEN_TEST_START` (WY2023) is ever read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    extra: tuple[tuple[str, int], ...] = ()
    product: str | None = None  # forecast: the init dimension


@dataclass(frozen=True)
class ForecastDims:
    init: str
    lead: str
    member: str | None


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
    # Arrays read concurrently per load_dynamic / load_forecast call
    read_threads: int = 1

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

    def forecast_dims(self, dims: tuple[str, ...]) -> ForecastDims | None:
        d = self.dims
        if d.issue in dims and d.lead in dims:
            return ForecastDims(d.issue, d.lead, d.member if d.member in dims else None)
        inits = [x for x in dims if x.endswith("_init")]
        for init in inits:
            p = init.removesuffix("_init")
            if f"{p}_lead" in dims:
                return ForecastDims(init, f"{p}_lead", f"{p}_member" if f"{p}_member" in dims else None)
        return None

    def _kind(self, dims: tuple[str, ...]) -> str | None:
        if self.dims.basin not in dims:
            return None
        if self.forecast_dims(dims) is not None:
            return "forecast"
        if self.dims.time in dims:
            return "dynamic"
        return "static"

    def _index_features(self) -> dict[str, list[FeatureRef]]:
        refs: dict[str, list[FeatureRef]] = {}
        core = {self.dims.basin, self.dims.time}
        for s, ds in enumerate(self.stores):
            for name, var in ds.data_vars.items():
                kind = self._kind(var.dims)
                if kind is None:
                    continue
                if kind == "forecast":
                    fd = self.forecast_dims(var.dims)
                    extra_dims = [x for x in var.dims if x not in (self.dims.basin, fd.init, fd.lead, fd.member)]
                    combos = [("", ())]
                    for dim in extra_dims:
                        coord = ds[dim].values if dim in ds.coords else np.arange(ds.sizes[dim])
                        combos = [(f"{suffix}_{dim}{c}", sel + ((dim, i),)) for suffix, sel in combos for i, c in enumerate(coord)]
                    for suffix, sel in combos:
                        refs.setdefault(f"{name}{suffix}", []).append(FeatureRef(kind, s, name, extra=sel, product=fd.init))
                    continue
                fdims = [x for x in var.dims if x in self.dims.feature]
                extra_dims = [x for x in var.dims if x not in core and x not in fdims]
                if len(fdims) > 1:
                    continue
                combos: list[tuple[str, tuple[tuple[str, int], ...]]] = [("", ())]
                for dim in extra_dims:
                    coord = ds[dim].values if dim in ds.coords else np.arange(ds.sizes[dim])
                    combos = [(f"{suffix}_{dim}{c}", sel + ((dim, i),)) for suffix, sel in combos for i, c in enumerate(coord)]
                for suffix, sel in combos:
                    if fdims:
                        for i, f in enumerate(ds[fdims[0]].values):
                            refs.setdefault(f"{f}{suffix}", []).append(FeatureRef(kind, s, name, fdims[0], i, sel))
                    else:
                        refs.setdefault(f"{name}{suffix}", []).append(FeatureRef(kind, s, name, extra=sel))
        return refs

    def forecast_product(self, feature: str) -> str:
        return self._refs[feature][0].product

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
        sel = {self.dims.basin: basin_pos, **isel, **dict(ref.extra)}
        if ref.feature_dim is not None:
            sel[ref.feature_dim] = ref.feature_index
        return np.asarray(var.isel(sel).values)

    def _read_all(self, reads: list[tuple[FeatureRef, int, dict]]) -> list[np.ndarray]:
        """`_read` of each (ref, basin position, isel), in order; concurrent with `read_threads` > 1 (the chunk decode
        releases the GIL). A pool per call, so a Cube built before DataLoader workers fork works in each of them."""
        if self.read_threads <= 1 or len(reads) <= 1:
            return [self._read(ref, pos, **isel) for ref, pos, isel in reads]
        with ThreadPoolExecutor(min(self.read_threads, len(reads))) as pool:
            return list(pool.map(lambda r: self._read(r[0], r[1], **r[2]), reads))

    def load_dynamic(self, basin: str, features: Iterable[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Hourly frame on the full [start, end] index; NaN where no store has data."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        self._check_time(end)
        index = pd.date_range(start, end, freq="h", name="date")
        features = list(features)
        plan: list[tuple[str, FeatureRef, int, int, int]] = []
        for f in features:
            if f not in self._refs:
                raise KeyError(f"feature {f!r} is not in the cube")
            for ref in self._refs[f]:
                if ref.kind != "dynamic":
                    raise ValueError(f"feature {f!r} is {ref.kind}, not dynamic")
                pos = self._basin_pos[ref.store].get(basin)
                times = self._times[ref.store]
                if pos is None or times is None:
                    continue
                lo, hi = times.searchsorted(start, "left"), times.searchsorted(end, "right")
                if hi > lo:
                    plan.append((f, ref, pos, lo, hi))
        arrays = self._read_all([(ref, pos, {self.dims.time: slice(lo, hi)}) for _, ref, pos, lo, hi in plan])
        targets: dict[tuple[int, int, int], np.ndarray] = {}
        columns = {f: (np.full(len(index), np.nan, dtype=np.float32), np.zeros(len(index), dtype=bool)) for f in features}
        for (f, ref, _, lo, hi), values in zip(plan, arrays):
            col, filled = columns[f]
            values = values.astype(np.float32)
            key = (ref.store, lo, hi)
            if key not in targets:
                targets[key] = index.get_indexer(self._times[ref.store][lo:hi])
            target = targets[key]
            ok = (target >= 0) & ~filled[np.clip(target, 0, None)]
            col[target[ok]] = values[ok]
            filled[target[ok]] |= ~np.isnan(values[ok])
        return pd.DataFrame({f: col for f, (col, _) in columns.items()}, index=index, dtype=np.float32)

    def load_static(self, basins: Sequence[str], features: Iterable[str]) -> pd.DataFrame:
        features = list(features)
        df = pd.DataFrame(index=pd.Index(list(basins), name="basin"), columns=features, dtype=np.float64)
        for f in features:
            for ref in self._refs.get(f, []):
                if ref.kind != "static":
                    raise ValueError(f"feature {f!r} is {ref.kind}, not static")
                ds = self.stores[ref.store]
                var = ds[ref.var].isel(dict(ref.extra))
                if ref.feature_dim is not None:
                    var = var.isel({ref.feature_dim: ref.feature_index})
                values = pd.Series(np.asarray(var.values, dtype=np.float64), index=[str(b) for b in ds[self.dims.basin].values])
                missing = df[f].isna()
                df.loc[missing, f] = values.reindex(df.index[missing]).to_numpy()
            if f not in self._refs:
                raise KeyError(f"static attribute {f!r} is not in the cube")
        return df

    def load_forecast(
        self,
        basin: str,
        features: Sequence[str],
        issue_start: pd.Timestamp,
        issue_end: pd.Timestamp,
        transform: Callable[[str, np.ndarray, np.ndarray], np.ndarray] | None = None,
        dtype: type = np.float32,
    ) -> dict[str, tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, list[str]]]:
        """Per product: (issue_times, leads_h, values[issue, lead, member, feature], feature names), issues in [start, end].

        `transform(feature, leads, x)` maps each feature's float32 x[issue, lead, member] (e.g. normalizes it) before it
        is stored as `dtype`, so a float16 result never needs a full float32 copy of the product.
        """
        issue_start, issue_end = pd.Timestamp(issue_start), pd.Timestamp(issue_end)
        groups: dict[tuple[int, str], list[str]] = {}
        for f in features:
            ref = self._refs[f][0]
            if ref.kind != "forecast":
                raise ValueError(f"{f!r} is not a forecast feature")
            groups.setdefault((ref.store, ref.product), []).append(f)
        out = {}
        for (store, product), names in groups.items():
            ds = self.stores[store]
            fd = self.forecast_dims(ds[self._refs[names[0]][0].var].dims)
            issues = pd.DatetimeIndex(ds[fd.init].values)
            leads = np.asarray(ds[fd.lead].values)
            if np.issubdtype(leads.dtype, np.timedelta64):
                leads = leads / np.timedelta64(1, "h")
            leads = leads.astype(float)
            lo, hi = issues.searchsorted(issue_start, "left"), issues.searchsorted(issue_end, "right")
            pos = self._basin_pos[store].get(basin)
            n_member = ds.sizes[fd.member] if fd.member else 1
            values = np.full((max(hi - lo, 0), len(leads), n_member, len(names)), np.nan, dtype=dtype)
            if pos is not None and hi > lo:
                # samples only use leads inside their own window, which ends inside the requested period
                self._check_time(issues[hi - 1])
                refs = [self._refs[f][0] for f in names]
                arrays = self._read_all([(ref, pos, {fd.init: slice(lo, hi)}) for ref in refs])
                for j, (f, ref, arr) in enumerate(zip(names, refs, arrays)):
                    picked = {d for d, _ in ref.extra}
                    dims = [d for d in ds[ref.var].dims if d != self.dims.basin and d not in picked]
                    if fd.member is None:
                        arr, dims = arr[..., None], [*dims, "_member"]
                    member = fd.member or "_member"
                    x = np.transpose(arr, [dims.index(fd.init), dims.index(fd.lead), dims.index(member)]).astype(np.float32, copy=False)
                    values[..., j] = transform(f, leads, x) if transform is not None else x
            out[product] = (issues[lo:hi], leads, values, names)
        return out

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
