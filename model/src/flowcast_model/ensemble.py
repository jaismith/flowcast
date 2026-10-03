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

import numpy as np
import pandas as pd

from .cube import Cube
from .hindcast import _long_frame
from .mixtures import PERFECT_SUFFIX, aligned_params, list_sites, read_mixture
from .units import to_harness_unit

log = logging.getLogger(__name__)


def sample_cmal(pi: np.ndarray, mu: np.ndarray, b: np.ndarray, tau: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """n draws per row from CMAL mixtures given as [rows, k] arrays: component by weight, then the asymmetric Laplace
    inverse CDF (the formula of hindcast.cmal_quantile). Returns [rows, n]."""
    rows, k = pi.shape
    cum = np.cumsum(pi / pi.sum(axis=1, keepdims=True), axis=1)
    comp = (rng.random((rows, n))[:, :, None] > cum[:, None, :]).sum(axis=2).clip(max=k - 1)
    m, s, t = (np.take_along_axis(a, comp, axis=1) for a in (mu, b, tau))
    u = rng.uniform(1e-6, 1 - 1e-6, (rows, n))
    return np.where(u < t, m + s * np.log(u / t) / (1 - t), m - s * np.log((1 - u) / (1 - t)) / t)


def pool_samples(frames: list[pd.DataFrame], per_member: int, rng: np.random.Generator) -> tuple[np.ndarray, pd.DatetimeIndex, np.ndarray, str]:
    """Samples [issue, lead, seed x forecast member x per_member] from the seeds' mixture frames of one site and mode,
    on the (issue, lead, forecast member) cells every seed has."""
    issues, leads, n_m, params = aligned_params(frames)
    draws = [sample_cmal(pi, mu, b, tau, per_member, rng).reshape(len(issues), len(leads), n_m * per_member) for pi, mu, b, tau in params]
    unit = str(frames[0]["unit"].iloc[0])
    return np.concatenate(draws, axis=2), issues, leads, unit


def _site_job(job) -> str | None:
    sources, site, stems, out, model, per_member, seed, area, variable = job
    rng = np.random.default_rng([seed, zlib.crc32(site.encode())])
    written = 0
    with tempfile.TemporaryDirectory() as tmp:
        for stem in stems:
            frames = [read_mixture(src, site, stem, Path(tmp) / str(i)) for i, src in enumerate(sources)]
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
