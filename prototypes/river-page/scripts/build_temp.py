"""Build the page's water-temperature forecasts from the production `temp_v2_nodrop` model's interim export.

    python scripts/build_temp.py [--src DIR] [--sites 01427510 01011000 01654000]

Reads `s3://flowcast-training-257129854363/page/interim/temp_v2_nodrop/USGS-<site>.parquet` (schema in the README
there): 12Z issues in WY2021-2022, hourly quantiles at leads 1-180 h, and daily-high quantiles for local days 0-7,
in degC, over 88 pooled members (2 seeds x 11 GEFS members x 4 sample paths). Writes
`../landing/public/data/sites/<site>/temp.json` (the page's public dir) with [issue][lead][quantile] arrays.

The export is uncalibrated. Production's plain calibration is small: hourly within +/-0.05 degC, daily highs
+0.16-0.18 degC at day 0 and within +/-0.06 degC at days 1-6. Only the day-0 high offset is applied here
(DAY0_HIGH_OFFSET_C); the rest is below the page's 1 degF rounding. No warm-up correction is applied.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import boto3
import numpy as np
import pandas as pd

BUCKET = "flowcast-training-257129854363"
PREFIX = "page/interim/temp_v2_nodrop"
SITES = ["01427510", "01011000", "01654000"]
QUANTS = ["q05", "q25", "q50", "q75", "q95"]
LEADS = np.arange(1, 181, dtype=float)
DAYS = 8
DAY0_HIGH_OFFSET_C = 0.17
VALIDATION_START = pd.Timestamp("2020-10-01T00:00", tz="UTC")
VALIDATION_END = pd.Timestamp("2022-09-30T23:00", tz="UTC")
OUT = Path(__file__).resolve().parents[2] / "landing" / "public" / "data" / "sites"


def fetch(site: str, src: Path) -> pd.DataFrame:
    path = src / f"USGS-{site}.parquet"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        boto3.client("s3").download_file(BUCKET, f"{PREFIX}/USGS-{site}.parquet", str(path))
    d = pd.read_parquet(path)
    for col in ("issue_time", "valid_time"):
        t = d[col]
        if t.min() < VALIDATION_START or t.max() > VALIDATION_END:
            raise RuntimeError(f"{site} {col}: outside WY2021-2022 ({t.min()} .. {t.max()}); the frozen test must stay unread")
    return d


def grid(rows: pd.DataFrame, issues: pd.DatetimeIndex, steps: np.ndarray) -> np.ndarray:
    """[issue, step, quantile] in degC, NaN where the export has no row."""
    full = pd.MultiIndex.from_product([issues, steps], names=["issue_time", "lead_h"])
    return rows.set_index(["issue_time", "lead_h"])[QUANTS].reindex(full).to_numpy(float, copy=True).reshape(len(issues), len(steps), len(QUANTS))


def jsonable(a: np.ndarray, decimals: int = 2) -> list:
    return [None if not math.isfinite(v) else round(v, decimals) for v in a.ravel().tolist()]


def build(site: str, src: Path) -> None:
    d = fetch(site, src)
    issues = pd.DatetimeIndex(sorted(d["issue_time"].unique()))
    assert (issues.hour == 12).all() and (issues.minute == 0).all(), "expected 12Z issues only"
    hourly = grid(d[d["variable"] == "water_temperature"], issues, LEADS)
    highs = grid(d[d["variable"] == "water_temperature_daily_max"], issues, np.arange(DAYS) * 24.0)
    highs[:, 0, :] += DAY0_HIGH_OFFSET_C
    out = OUT / site / "temp.json"
    out.write_text(json.dumps({
        "model": "temp_v2_nodrop (production), seeds 42 + 43, 88 members (11 GEFS x 4 sample paths per seed), CPU rerun with hourly leads",
        "calibration": f"day-0 high +{DAY0_HIGH_OFFSET_C} degC; otherwise uncalibrated (production calibration is within +/-0.05 degC hourly, +/-0.06 degC on days 1-6)",
        "units": "degC",
        "issues": ((issues - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).tolist(),
        "leads": LEADS.tolist(),
        "quants": [0.05, 0.25, 0.5, 0.75, 0.95],
        "hourly": jsonable(hourly),
        "highs": jsonable(highs),
    }, separators=(",", ":"), allow_nan=False))
    missing = int(np.isnan(hourly[..., 2]).sum()), int(np.isnan(highs[..., 2]).sum())
    print(f"{out}: {len(issues)} issues, {out.stat().st_size / 1e6:.1f} MB, missing hourly/daily medians {missing[0]}/{missing[1]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=Path("/tmp/temp_v2_nodrop"), help="local cache of the S3 export")
    ap.add_argument("--sites", nargs="*", default=SITES)
    args = ap.parse_args()
    for site in args.sites:
        build(site, args.src)


if __name__ == "__main__":
    main()
