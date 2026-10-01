"""Exact CRPS, quantiles and score pairs of pooled CMAL mixtures, without sampling.

A pooled forecast is the equal-weight mixture over seeds and forecast (GEFS) members of each one's CMAL mixture, so
per issue and lead it is one mixture of C = seeds x members x k asymmetric Laplace components X_c (the
NeuralHydrology parameterization, see `hindcast.cmal_cdf`), clipped at zero like the hindcast: Z = max(X, 0).

CRPS has a closed form: CRPS(F, y) = E|Z - y| - E|Z - Z'| / 2, with
  E|Z - y|   = sum_c w_c E|Z_c - y|                  one exponential per component,
  E|Z - Z'|  = sum_c sum_d w_c w_d E|Z_c - Z_d|       pairwise, two exponentials per pair.
Each term is a piecewise-exponential integral of the components' CDFs over [0, inf), split at 0 and the components'
locations, so nothing is approximated. The cost is O(C^2) per cell (4,851 pairs for 3 seeds x 11 members x 3
components). Quantiles are the clipped mixture's (safeguarded Newton on the CDF, to 1e-12 relative), so the median,
the 10-90% interval and its coverage are exact too. The unit conversion is linear, so it scales the results.
"""

from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from flowcast_eval.pairs import PAIR_COLUMNS, lookup_obs

from .mixtures import PERFECT_SUFFIX, aligned_params, list_sites, read_mixture
from .units import to_cfs

log = logging.getLogger(__name__)
LEVELS = np.array([0.1, 0.5, 0.9])


@njit(cache=True, inline="always", fastmath=True)
def _pair(mc, ac, bc, tc, lc, uc, zc, md, ad, bd, td, ld, ud, zd):
    """E|Z_c - Z_d| for two clipped asymmetric Laplace components with mc <= md.

    a, b are the lower and upper tail rates ((1 - tau) / scale, tau / scale), l = tau / a and u = (1 - tau) / b their
    tail integrals, z the component's tail factor at 0 (exp(-a mu) if mu > 0, else exp(b mu)). The integrand
    F_c (1 - F_d) + F_d (1 - F_c) is integrated over [0, mc), [max(0, mc), md) and [max(0, md), inf)."""
    delta = md - mc
    edc = math.exp(-ad * delta)
    ecd = math.exp(-bc * delta)
    s = 0.0
    if mc > 0.0:
        s += lc * (1.0 - zc) + ld * (edc - zd) - 2.0 * tc * td / (ac + ad) * (edc - zc * zd)
    if md > 0.0:
        if mc >= 0.0:
            h, pl, ql, xl = delta, 1.0, edc, edc
        else:
            h, pl, ql, xl = md, zc, zd, zc * zd
        lam = ad - bc
        if abs(lam * h) < 1e-6:
            cross = h * math.sqrt(xl * ecd)
        else:
            cross = (ecd - xl) / lam
        s += h - ld * (1.0 - ql) - uc * (pl - ecd) + 2.0 * (1.0 - tc) * td * cross
        pm, qm = ecd, 1.0
    else:
        pm, qm = zc, zd
    s += uc * pm + ud * qm - 2.0 * (1.0 - tc) * (1.0 - td) / (bc + bd) * pm * qm
    return s


@njit(cache=True, inline="always", fastmath=True)
def _abs_dev(m, a, b, t, tl, tu, z, y):
    """E|Z_c - y| for one clipped component and y >= 0."""
    if y <= m:
        ey = math.exp(a * (y - m))
        return tl * (ey - z) + (m - y) - tl * (1.0 - ey) + tu
    ey = math.exp(-b * (y - m))
    below = tl * (1.0 - z) + (y - m) - tu * (1.0 - ey) if m > 0.0 else y - tu * (z - ey)
    return below + tu * ey


@njit(cache=True, inline="always", fastmath=True)
def _cdf_pdf(x, w, m, a, b, t, n):
    cdf, pdf = 0.0, 0.0
    for c in range(n):
        if x < m[c]:
            e = t[c] * math.exp(a[c] * (x - m[c]))
            cdf += w[c] * e
            pdf += w[c] * a[c] * e
        else:
            e = (1.0 - t[c]) * math.exp(-b[c] * (x - m[c]))
            cdf += w[c] * (1.0 - e)
            pdf += w[c] * b[c] * e
    return cdf, pdf


@njit(cache=True, fastmath=True)
def _quantile(v, w, m, a, b, t, n, cdf0):
    """Quantile of the clipped mixture at level v; between the components' own v-quantiles."""
    if cdf0 >= v:
        return 0.0
    lo, hi, x = np.inf, -np.inf, 0.0
    for c in range(n):
        q = m[c] + math.log(v / t[c]) / a[c] if v < t[c] else m[c] - math.log((1.0 - v) / (1.0 - t[c])) / b[c]
        lo, hi, x = min(lo, q), max(hi, q), x + w[c] * q
    lo = max(lo, 0.0)
    x = min(max(x, lo), hi)
    for _ in range(200):
        cdf, pdf = _cdf_pdf(x, w, m, a, b, t, n)
        if cdf < v:
            lo = x
        else:
            hi = x
        step = (cdf - v) / pdf if pdf > 0.0 else np.inf
        nxt = x - step
        if not (lo < nxt < hi):
            nxt = 0.5 * (lo + hi)
        if abs(nxt - x) <= 1e-12 * abs(nxt) or hi - lo <= 1e-12 * abs(hi):
            return nxt
        x = nxt
    return x


@njit(cache=True, fastmath=True)
def mixture_scores(w, mu, scale, tau, y, levels):
    """CRPS against y and quantiles at `levels` of clipped mixtures given row-wise as [rows, C] arrays.

    Weights are normalized per row. Rows with a non-finite y get NaN CRPS (quantiles are still computed)."""
    rows, n = w.shape
    crps = np.full(rows, np.nan)
    quant = np.empty((rows, len(levels)))
    work = np.empty((8, n))
    m, a, b, t, ww, tl, tu, z = work[0], work[1], work[2], work[3], work[4], work[5], work[6], work[7]
    for r in range(rows):
        order = np.argsort(mu[r])
        total = 0.0
        for c in range(n):
            total += w[r, c]
        cdf0 = 0.0
        for j in range(n):
            c = order[j]
            m[j], t[j], ww[j] = mu[r, c], tau[r, c], w[r, c] / total
            a[j], b[j] = (1.0 - t[j]) / scale[r, c], t[j] / scale[r, c]
            tl[j], tu[j] = t[j] / a[j], (1.0 - t[j]) / b[j]
            z[j] = math.exp(-a[j] * m[j]) if m[j] > 0.0 else math.exp(b[j] * m[j])
            cdf0 += ww[j] * (t[j] * z[j] if m[j] > 0.0 else 1.0 - (1.0 - t[j]) * z[j])
        for k in range(len(levels)):
            quant[r, k] = _quantile(levels[k], ww, m, a, b, t, n, cdf0)
        yr = y[r]
        if not np.isfinite(yr):
            continue
        yc = max(yr, 0.0)
        dev, spread = 0.0, 0.0
        for c in range(n):
            if ww[c] == 0.0:
                continue
            dev += ww[c] * _abs_dev(m[c], a[c], b[c], t[c], tl[c], tu[c], z[c], yc)
            row = 0.0
            for d in range(c + 1, n):
                if ww[d] != 0.0:
                    row += ww[d] * _pair(m[c], a[c], b[c], t[c], tl[c], tu[c], z[c], m[d], a[d], b[d], t[d], tl[d], tu[d], z[d])
            spread += ww[c] * (2.0 * row + ww[c] * _pair(m[c], a[c], b[c], t[c], tl[c], tu[c], z[c], m[c], a[c], b[c], t[c], tl[c], tu[c], z[c]))
        crps[r] = dev + (yc - yr) - 0.5 * spread
    return crps, quant


def pooled_mixture(frames: list[pd.DataFrame]) -> tuple[pd.DatetimeIndex, np.ndarray, tuple[np.ndarray, ...], str]:
    """(issues, leads, (w, mu, b, tau) as [issue, lead, seeds x members x k], unit) of the seeds' mixture frames of one
    site and mode, on the cells every seed has; every seed and member weighs the same."""
    issues, leads, n_m, params = aligned_params(frames)
    n_i, n_l, n_s = len(issues), len(leads), len(params)
    width = n_m * params[0][0].shape[1]
    out = np.empty((4, n_i, n_l, n_s * width))
    for j, (pi, mu, b, tau) in enumerate(params):
        for dest, x in zip(out, (pi / pi.sum(axis=1, keepdims=True) / (n_s * n_m), mu, b, tau)):
            dest[:, :, j * width : (j + 1) * width] = x.reshape(n_i, n_l, width)
    return issues, leads, tuple(out), str(frames[0]["unit"].iloc[0])


def scored_cells(frame: pd.DataFrame, leads_h: tuple[float, ...], issue_hours: tuple[int, ...], window: tuple[pd.Timestamp, pd.Timestamp]) -> pd.DataFrame:
    """The rows of a mixture frame that the protocol scores: cycle issues inside `window`, at `leads_h`."""
    t = frame["issue_time"].dt
    keep = t.hour.isin(issue_hours) & (t.minute == 0) & (frame["issue_time"] >= window[0]) & (frame["issue_time"] <= window[1]) & frame["lead_h"].isin(leads_h)
    return frame[keep]


def mixture_pairs(frames: list[pd.DataFrame], obs: pd.Series, site: str, model: str, run_type: str, area_km2: float | None, end: pd.Timestamp, variable: str = "discharge") -> pd.DataFrame:
    """Score pairs (median, exact CRPS, 10-90% interval, obs; `flowcast_eval.pairs.PAIR_COLUMNS`) in ft3/s of the
    pooled mixtures of one site and mode, from the seeds' frames cut to `scored_cells`; no obs after `end`."""
    if any(f.empty for f in frames):
        return pd.DataFrame(columns=PAIR_COLUMNS)
    issues, leads, (w, mu, b, tau), unit = pooled_mixture(frames)
    w, mu, b, tau = (x.reshape(len(issues) * len(leads), -1) for x in (w, mu, b, tau))
    valid = pd.DatetimeIndex((issues.values[:, None] + (leads * 3600e9).astype("timedelta64[ns]")[None, :]).ravel()).tz_localize("UTC")
    o = lookup_obs(obs, valid)
    o[valid > end] = np.nan
    factor = float(to_cfs(np.ones(1), unit, area_km2)[0])
    crps, q = mixture_scores(w, mu, b, tau, o / factor, LEVELS)
    pairs = pd.DataFrame(
        {
            "model": model,
            "site_id": site,
            "variable": variable,
            "run_type": run_type,
            "issue_time": np.repeat(issues, len(leads)),
            "valid_time": valid,
            "lead_h": np.tile(leads.astype(float), len(issues)),
            "point": q[:, 1] * factor,
            "crps": crps * factor,
            "lo": q[:, 0] * factor,
            "hi": q[:, 2] * factor,
            "obs": o,
        }
    )
    return pairs[PAIR_COLUMNS]


def mixture_pools(mixtures: dict[str, list[str]]) -> dict[str, dict[str, tuple[list[str], list[str]]]]:
    """{site: {model: (sources, file stems)}} for the sites and stems every seed of a pool has."""
    out: dict[str, dict[str, tuple[list[str], list[str]]]] = {}
    for model, sources in mixtures.items():
        listings = [list_sites(str(s)) for s in sources]
        every = set.intersection(*(set(x) for x in listings))
        for site in sorted(set.union(*(set(x) for x in listings)) - every):
            log.warning("%s: %s mixtures missing in %d of %d runs, skipped", site, model, sum(site not in x for x in listings), len(listings))
        for site in sorted(every):
            stems = sorted(set.intersection(*(set(x[site]) for x in listings)))
            if stems:
                out.setdefault(site, {})[model] = ([str(s) for s in sources], stems)
    return out


def site_mixture_pairs(pools: dict[str, tuple[list[str], list[str]]], site: str, obs: pd.Series, area_km2: float | None, leads_h, issue_hours, window) -> pd.DataFrame:
    """Pairs of every pool {model: (seed mixture sources, file stems)} at one site; a stem missing from a seed is skipped."""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for model, (sources, stems) in pools.items():
            for stem in stems:
                frames = []
                for i, src in enumerate(sources):
                    frame = read_mixture(src, site, stem, Path(tmp) / str(i))
                    frames.append(None if frame is None else scored_cells(frame, leads_h, issue_hours, window))
                    del frame
                if any(f is None for f in frames):
                    log.warning("%s %s: missing in %d of %d runs, skipped", site, stem, sum(f is None for f in frames), len(frames))
                    continue
                perfect = stem.endswith(PERFECT_SUFFIX)
                name = f"{model}{PERFECT_SUFFIX if perfect else ''}"
                out.append(mixture_pairs(frames, obs, site, name, "perfect_forcing" if perfect else "operational", area_km2, window[1]))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=PAIR_COLUMNS)
