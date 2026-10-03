"""Multi-seed ensembles from saved CMAL mixtures (hindcast `save_mixture`), on CPU.

A seed ensemble's predictive distribution is the equal-weight mixture of the seeds' mixtures: for each issue, lead
and forecast (GEFS) member, every seed contributes its CMAL mixture. Samples are drawn stratified, the same number
from every seed and forecast member, so they follow that pooled mixture. They are clipped at zero (as the hindcast
does), converted to the harness unit and written in the hindcast's long format, one file per site and hindcast mode,
so the usual scorers read them. Sources are `<run>/run/hindcast_mixture` folders, local or s3://; sites are streamed
one at a time, so the seeds' mixtures (about 23 GB each for 553 basins) never have to fit on disk together.
"""

from __future__ import annotations

import logging
import multiprocessing
import tempfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError

from .cube import Cube
from .hindcast import _long_frame
from .units import to_harness_unit

log = logging.getLogger(__name__)
PERFECT_SUFFIX = "_perfect"


def sample_cmal(pi: np.ndarray, mu: np.ndarray, b: np.ndarray, tau: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """n draws per row from CMAL mixtures given as [rows, k] arrays: component by weight, then the asymmetric Laplace
    inverse CDF (the formula of hindcast.cmal_quantile). Returns [rows, n]."""
    rows, k = pi.shape
    cum = np.cumsum(pi / pi.sum(axis=1, keepdims=True), axis=1)
    comp = (rng.random((rows, n))[:, :, None] > cum[:, None, :]).sum(axis=2).clip(max=k - 1)
    m, s, t = (np.take_along_axis(a, comp, axis=1) for a in (mu, b, tau))
    u = rng.uniform(1e-6, 1 - 1e-6, (rows, n))
    return np.where(u < t, m + s * np.log(u / t) / (1 - t), m - s * np.log((1 - u) / (1 - t)) / t)


def _params(frame: pd.DataFrame) -> tuple[np.ndarray, ...]:
    k = sum(1 for c in frame.columns if c.startswith("pi"))
    return tuple(frame[[f"{name}{c}" for c in range(k)]].to_numpy(np.float64) for name in ("pi", "mu", "b", "tau"))


def pool_samples(frames: list[pd.DataFrame], per_member: int, rng: np.random.Generator) -> tuple[np.ndarray, pd.DatetimeIndex, np.ndarray, str]:
    """Samples [issue, lead, seed x forecast member x per_member] from the seeds' mixture frames of one site and mode,
    on the (issue, lead, forecast member) cells every seed has."""
    keys = ["issue_time", "lead_h", "member"]
    common = frames[0][keys]
    for f in frames[1:]:
        common = common.merge(f[keys], on=keys)
    common = common.sort_values(keys).reset_index(drop=True)
    issues = pd.DatetimeIndex(sorted(common["issue_time"].unique()))
    leads = np.sort(common["lead_h"].unique())
    members = np.sort(common["member"].unique())
    n_i, n_l, n_m = len(issues), len(leads), len(members)
    if len(common) != n_i * n_l * n_m:
        raise ValueError("mixture cells don't form a full issue x lead x member grid")
    draws = []
    for f in frames:
        aligned = common.merge(f, on=keys, how="left")
        pi, mu, b, tau = _params(aligned)
        draws.append(sample_cmal(pi, mu, b, tau, per_member, rng).reshape(n_i, n_l, n_m * per_member))
    unit = str(frames[0]["unit"].iloc[0])
    return np.concatenate(draws, axis=2), issues, leads, unit


def _read(source: str, site: str, name: str, tmp: Path) -> pd.DataFrame | None:
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


def _site_job(job) -> str | None:
    sources, site, stems, out, model, per_member, seed, area, variable = job
    rng = np.random.default_rng([seed, zlib.crc32(site.encode())])
    written = 0
    with tempfile.TemporaryDirectory() as tmp:
        for stem in stems:
            frames = [_read(src, site, stem, Path(tmp) / str(i)) for i, src in enumerate(sources)]
            if any(f is None for f in frames):
                log.warning("%s %s: missing in %d of %d runs, skipped", site, stem, sum(f is None for f in frames), len(frames))
                continue
            values, issues, leads, unit = pool_samples(frames, per_member, rng)
            values, out_unit = to_harness_unit(np.clip(values, 0.0, None), unit, area)
            perfect = stem.endswith(PERFECT_SUFFIX)
            name = f"{model}{PERFECT_SUFFIX if perfect else ''}"
            frame = _long_frame(values.astype(np.float32), issues.floor("h"), issues, leads.astype(int), variable, name, out_unit, "perfect_forcing" if perfect else "operational")
            target = Path(out) / f"site_id={site}" / f"{name}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_suffix(".parquet.tmp")
            frame.to_parquet(tmp_target, index=False)
            tmp_target.replace(target)
            written += 1
    return site if written else None


def mixture_ensemble(
    sources: list[str],
    out: str | Path,
    model: str,
    cube_paths: list[str],
    per_member: int = 4,
    seed: int = 0,
    sites: list[str] | None = None,
    workers: int = 1,
    variable: str = "discharge",
) -> list[str]:
    """Pool the seeds' mixtures into sampled ensemble hindcasts in `out`; returns the sites written."""
    listing = list_sites(sources[0])
    chosen = sorted(set(sites) & set(listing) if sites else listing)
    basins = [s.removeprefix("USGS-") for s in chosen]
    cube = Cube(cube_paths)
    known = [b for b in basins if b in set(cube.basins)]
    areas = cube.load_static(known, ["area_km2"])["area_km2"] if known else pd.Series(dtype=float)
    jobs = []
    for site, basin in zip(chosen, basins):
        if basin not in areas.index:
            log.warning("%s: no drainage area in the cube, skipped", site)
            continue
        jobs.append(([str(s) for s in sources], site, listing[site], str(out), model, per_member, seed, float(areas[basin]), variable))
    if workers > 1:
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            done = list(pool.map(_site_job, jobs))
    else:
        done = [_site_job(j) for j in jobs]
    written = [s for s in done if s]
    log.info("pooled %d runs into %d sites in %s", len(sources), len(written), out)
    return written
