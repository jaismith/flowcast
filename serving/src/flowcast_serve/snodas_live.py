"""Daily SNODAS for the served sites (production-architecture.md §2.1), with the cube's weights and timing rules.

One NSIDC G02158 masked tarball per day covers every basin. Each site's AORC basin and elevation-band weights
(`weights_aorc_all.npz`, the cube's) are remapped onto the SNODAS grid at onboarding
(`sites/USGS-{id}/serving/weights/snodas.npz`) and reduced with the cube builder's `Reducer`. Daily rows go to
`forcing/snodas/USGS-{id}/{YYYY}.parquet`; hourly model inputs use the cube's availability rule
(`hourly_positions`: day D's 06 UTC product from 00 UTC on D+1, carried forward up to 7 days).
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from flowcast_pipeline.dataset.snodas import (
    GRID,
    MAX_AGE_DAYS,
    Reducer,
    hourly_positions,
    nsidc_day,
    reduce_fields,
)
from flowcast_pipeline.lake import Lake

log = logging.getLogger(__name__)

N_BANDS = 4
COLUMNS = ["swe_mm", *[f"swe_mm_band{k}" for k in range(N_BANDS)], "depth_mm", "melt_mm_h"]


def weights_key(basin: str) -> str:
    return f"sites/USGS-{basin}/serving/weights/snodas.npz"


def daily_key(basin: str, year: int) -> str:
    return f"forcing/snodas/USGS-{basin}/{year}.parquet"


def load_weights(lake: Lake, basin: str) -> sp.csr_matrix:
    data = lake.read(weights_key(basin))
    if data is None:
        raise FileNotFoundError(f"{weights_key(basin)} is missing; run `flowcast-serve onboard`")
    return sp.load_npz(io.BytesIO(data)).tocsr()


def save_weights(lake: Lake, basin: str, w: sp.csr_matrix) -> None:
    buf = io.BytesIO()
    sp.save_npz(buf, w.tocsr())
    lake.write(weights_key(basin), buf.getvalue(), "application/octet-stream")


def load_daily(lake: Lake, basin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    frames = [f for f in (lake.read_parquet(daily_key(basin, y)) for y in range(start.year, end.year + 1)) if f is not None]
    if not frames:
        return pd.DataFrame(columns=["day", *COLUMNS])
    df = pd.concat(frames).drop_duplicates("day", keep="last").sort_values("day")
    return df[(df["day"] >= start.tz_convert(None).normalize()) & (df["day"] <= end.tz_convert(None))].reset_index(drop=True)


def ingest(lake: Lake, basins: list[str], end: pd.Timestamp, days_back: int = 35, cache: Path = Path("/tmp/flowcast/snodas")) -> dict:
    """Fetch every day in [end - days_back, end] that some basin lacks; returns {"added": [...], "missing": [...]}."""
    cache.mkdir(parents=True, exist_ok=True)
    first = (end - pd.Timedelta(days=days_back)).tz_convert(None).normalize()
    days = pd.date_range(first, end.tz_convert(None).normalize(), freq="D")
    with ThreadPoolExecutor(16) as pool:
        loaded = list(pool.map(lambda b: set(pd.DatetimeIndex(load_daily(lake, b, pd.Timestamp(first, tz="UTC"), end)["day"])), basins))
    have = dict(zip(basins, loaded, strict=True))
    todo = [d for d in days if any(d not in have[b] for b in basins) and d + pd.Timedelta(hours=13) <= end.tz_convert(None)]
    if not todo:
        return {"added": [], "missing": []}
    with ThreadPoolExecutor(16) as pool:
        weights = dict(zip(basins, pool.map(lambda b: load_weights(lake, b), basins), strict=True))
    order = list(weights)
    reducer = Reducer(sp.vstack([weights[b] for b in order]).tocsr(), GRID)
    rows: dict[str, list[dict]] = {b: [] for b in order}
    added, missing = [], []
    for day in todo:
        fields = nsidc_day(day, cache)
        if fields is None:
            missing.append(str(day.date()))
            continue
        sums = reduce_fields(reducer, {var: x[None] for var, x in fields.items()})
        vals = {var: reducer.finish(*nd)[:, 0] for var, nd in sums.items()}
        r0 = 0
        for b in order:
            n = weights[b].shape[0]  # basin row, then its bands
            swe, depth, melt = vals["swe"][r0 : r0 + n], vals["depth"][r0 : r0 + n], vals["melt"][r0 : r0 + n]
            rows[b].append({"day": day, "swe_mm": swe[0], **{f"swe_mm_band{k}": swe[1 + k] for k in range(N_BANDS)}, "depth_mm": depth[0], "melt_mm_h": melt[0]})
            r0 += n
        added.append(str(day.date()))
        (cache / f"SNODAS_{day:%Y%m%d}.tar").unlink(missing_ok=True)
    for b in order:
        if not rows[b]:
            continue
        new = pd.DataFrame(rows[b])
        for year, part in new.groupby(new["day"].dt.year):
            old = lake.read_parquet(daily_key(b, int(year)))
            if old is not None:
                part = pd.concat([old[~old["day"].isin(part["day"])], part], ignore_index=True)
            lake.write_parquet(daily_key(b, int(year)), part.sort_values("day").reset_index(drop=True))
    return {"added": added, "missing": missing}


def hourly(lake: Lake, basin: str, index: pd.DatetimeIndex, now: pd.Timestamp) -> pd.DataFrame:
    """Hourly `snodas_swe_mm` and band SWE on `index` (UTC) as the cube built them, using only products out by `now`."""
    daily = load_daily(lake, basin, index[0] - pd.Timedelta(days=MAX_AGE_DAYS + 2), index[-1])
    out = pd.DataFrame(np.nan, index=index, columns=["snodas_swe_mm", *[f"snodas_band_swe_mm_band{k}" for k in range(N_BANDS)]], dtype=np.float32)
    if daily.empty:
        return out
    days = pd.DatetimeIndex(daily["day"])
    have = np.isfinite(daily["swe_mm"].to_numpy(float))
    src, _ = hourly_positions(days, have, index, now)
    ok = src >= 0
    out.loc[ok, "snodas_swe_mm"] = daily["swe_mm"].to_numpy(np.float32)[src[ok]]
    for k in range(N_BANDS):
        out.loc[ok, f"snodas_band_swe_mm_band{k}"] = daily[f"swe_mm_band{k}"].to_numpy(np.float32)[src[ok]]
    return out
