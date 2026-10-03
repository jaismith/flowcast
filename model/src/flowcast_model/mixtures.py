"""Saved CMAL mixtures (hindcast `save_mixture`): reading runs' mixture folders, local or s3://, and aligning seeds.

Kept free of torch so the CPU tools that pool or score mixtures (`ensemble`, `mixscore`) start light.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError

PERFECT_SUFFIX = "_perfect"
KEYS = ["issue_time", "lead_h", "member"]


def cmal_params(frame: pd.DataFrame) -> tuple[np.ndarray, ...]:
    """(pi, mu, b, tau) as [rows, k] arrays of a mixture frame."""
    k = sum(1 for c in frame.columns if c.startswith("pi"))
    return tuple(frame[[f"{name}{c}" for c in range(k)]].to_numpy(np.float64) for name in ("pi", "mu", "b", "tau"))


def aligned_params(frames: list[pd.DataFrame]) -> tuple[pd.DatetimeIndex, np.ndarray, int, list[tuple[np.ndarray, ...]]]:
    """(issues, leads, number of forecast members, per seed (pi, mu, b, tau) as [issue x lead x member, k]) of the seeds'
    mixture frames of one site and mode, on the (issue, lead, forecast member) cells every seed has."""
    frames = [f.sort_values(KEYS, ignore_index=True) for f in frames]
    common = frames[0][KEYS]
    same = all(len(f) == len(common) and all(np.array_equal(f[k].to_numpy(), common[k].to_numpy()) for k in KEYS) for f in frames[1:])
    if not same:
        for f in frames[1:]:
            common = common.merge(f[KEYS], on=KEYS)
        common = common.sort_values(KEYS, ignore_index=True)
        frames = [common.merge(f, on=KEYS, how="left") for f in frames]
    issues = pd.DatetimeIndex(common["issue_time"].unique())
    leads = np.sort(common["lead_h"].unique())
    n_m = common["member"].nunique()
    if len(common) != len(issues) * len(leads) * n_m:
        raise ValueError("mixture cells don't form a full issue x lead x member grid")
    return issues, leads, n_m, [cmal_params(f) for f in frames]


def read_mixture(source: str, site: str, name: str, tmp: Path) -> pd.DataFrame | None:
    """One site's mixture file `name` from a run's mixture folder; None if the run doesn't have it."""
    rel = f"site_id={site}/{name}.parquet"
    if source.startswith("s3://"):
        bucket, _, prefix = source[5:].partition("/")
        target = tmp / f"{name}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            boto3.client("s3").download_file(bucket, f"{prefix.rstrip('/')}/{rel}", str(target))
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        frame = pd.read_parquet(target)
        target.unlink()
        return frame
    path = Path(source) / rel
    return pd.read_parquet(path) if path.exists() else None


def list_sites(source: str) -> dict[str, list[str]]:
    """{site_id: [mixture file stems]} of one run's mixture folder."""
    if not source.startswith("s3://"):
        return {p.name.split("=", 1)[1]: sorted(f.stem for f in p.glob("*.parquet")) for p in sorted(Path(source).glob("site_id=*"))}
    bucket, _, prefix = source[5:].partition("/")
    prefix = prefix.rstrip("/") + "/"
    out: dict[str, list[str]] = {}
    for page in boto3.client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if rel.startswith("site_id=") and rel.endswith(".parquet") and rel.count("/") == 1:
                site, name = rel.split("/")
                out.setdefault(site.split("=", 1)[1], []).append(name.removesuffix(".parquet"))
    return {s: sorted(v) for s, v in out.items()}
