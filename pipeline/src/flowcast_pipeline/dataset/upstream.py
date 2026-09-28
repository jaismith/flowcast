"""Upstream gauged sub-basins for every training basin (rebuild plan §3: upstream tributary gauges as inputs).

Discovery uses NLDI (upstream-tributary navigation over NHDPlusV2 from each basin's gauge) to list every NWIS
site upstream; candidates are USGS gauges with instantaneous discharge, enough hourly history and at least
`MIN_AREA_KM2` of drainage. Topology, drainage area, along-channel distance and travel time come from the
NHDPlusV2 value-added attributes (`pathlength`, `pathtimema`, `hydroseq`/`dnhydroseq`, `totdasqkm`).

To avoid double counting, only *outermost* gauges enter the sum: a candidate is dropped when another candidate
lies downstream of it on the way to the target (its water is already in that gauge's flow).
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite"
VAA_URL = "https://www.hydroshare.org/resource/6092c8a62fac45be97a09bfd0b0bf726/data/contents/nhdplusVAA.parquet"
VAA_COLUMNS = ["comid", "hydroseq", "dnhydroseq", "pathlength", "lengthkm", "frommeas", "tomeas", "totdasqkm", "areasqkm", "totma", "wbareacomi"]
# NHDPlus EROM "velocities" on waterbody flowlines reflect reservoir residence time (~6,000 h through Cannonsville),
# not how fast a flow change propagates, so waterbody reaches are timed at this wave celerity instead.
WATERBODY_CELERITY_KMH = 3.6
MIN_AREA_KM2 = 10.0
MAX_AREA_FRAC = 0.98  # a gauge with nearly the target's area is the target (or co-located)
MIN_RECORD_YEARS = 5.0
RECORD_SINCE = pd.Timestamp("2000-01-01", tz="UTC")
MUST_BEGIN_BY = pd.Timestamp("2019-10-01", tz="UTC")  # some record inside the training years
N_SLOTS = 3


def _get(url: str, params: dict | None = None, retries: int = 6) -> requests.Response:
    headers = {"User-Agent": "flowcast/0.1 (+https://github.com/jaismith/flowcast)"}
    if key := os.environ.get("API_DATA_GOV_KEY"):
        headers["X-Api-Key"] = key
    for attempt in range(retries):
        resp = requests.get(url, params=params, headers=headers, timeout=120)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = float(resp.headers.get("Retry-After", 0) or 0) or 5 * 2**attempt
            log.warning("NLDI %s -> %s, retry in %.0fs", url, resp.status_code, wait)
            time.sleep(min(wait, 600))
            continue
        return resp
    return resp


def nldi_json(cache: Path, name: str, url: str, params: dict | None = None) -> dict | None:
    path = cache / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    resp = _get(url, params)
    if resp.status_code == 404:
        data = None
    else:
        resp.raise_for_status()
        data = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def site_feature(cache: Path, site: str) -> dict | None:
    data = nldi_json(cache, f"site/{site}", f"{NLDI}/USGS-{site}", {"f": "json"})
    return data["features"][0]["properties"] if data and data.get("features") else None


def upstream_sites(cache: Path, site: str) -> list[dict]:
    data = nldi_json(cache, f"ut/{site}", f"{NLDI}/USGS-{site}/navigation/UT/nwissite", {"distance": 9999, "f": "json"})
    if not data:
        return []
    return [f["properties"] for f in data.get("features", []) if f["properties"]["identifier"].startswith("USGS-")]


class Network:
    """NHDPlusV2 flowline topology and path attributes."""

    def __init__(self, vaa: pd.DataFrame):
        v = vaa.set_index("comid")
        self.v = v
        self.by_hydroseq = pd.Series(v.index.to_numpy(), index=v["hydroseq"].to_numpy())

    @classmethod
    def load(cls, path: Path) -> "Network":
        return cls(pd.read_parquet(path, columns=VAA_COLUMNS))

    def _frac(self, comid: int, measure: float) -> float:
        """Fraction of the flowline's length that lies upstream of `measure` (measures run 0 at the downstream end)."""
        r = self.v.loc[comid]
        span = r["tomeas"] - r["frommeas"]
        return float(np.clip((measure - r["frommeas"]) / span, 0, 1)) if span > 0 else 0.5

    def position(self, comid: int, measure: float) -> tuple[float, float]:
        """(km to network terminus, drainage area km2) at a point on a flowline."""
        r = self.v.loc[comid]
        f = self._frac(comid, measure)
        km = r["pathlength"] + r["lengthkm"] * f
        # Drainage at the point excludes the part of the local catchment that lies downstream of it.
        area = r["totdasqkm"] - r["areasqkm"] * (1 - f) if r["totdasqkm"] > 0 else np.nan
        return float(km), float(area)

    def _hours(self, comid: int, share: float) -> float:
        r = self.v.loc[comid]
        if r["wbareacomi"] > 0 or not r["totma"] > 0:
            return share * r["lengthkm"] / WATERBODY_CELERITY_KMH
        return share * r["totma"] * 24.0

    def travel_hours(self, path: list[int], up_measure: float, down_measure: float) -> float:
        """Mean-annual travel time along `path` from a point on its first flowline to a point on its last."""
        if len(path) == 1:
            return self._hours(path[0], abs(self._frac(path[0], up_measure) - self._frac(path[0], down_measure)))
        total = self._hours(path[0], self._frac(path[0], up_measure))
        total += sum(self._hours(c, 1.0) for c in path[1:-1])
        return total + self._hours(path[-1], 1.0 - self._frac(path[-1], down_measure))

    def path(self, comid: int, target: int, max_steps: int = 50_000) -> list[int] | None:
        """Flowlines from `comid` down the main path to `target` (inclusive), or None if target isn't downstream."""
        if comid not in self.v.index or target not in self.v.index:
            return None
        stop = self.v.at[target, "hydroseq"]
        out, cur = [comid], comid
        for _ in range(max_steps):
            if cur == target:
                return out
            hs = self.v.at[cur, "hydroseq"]
            dn = self.v.at[cur, "dnhydroseq"]
            if dn <= 0 or hs < stop or dn not in self.by_hydroseq.index:
                return None
            cur = int(self.by_hydroseq.loc[dn])
            out.append(cur)
        return None


@dataclass
class Gauge:
    site: str
    comid: int
    measure: float
    area_km2: float
    distance_km: float
    travel_time_h: float
    record_years: float
    outermost: bool = False


def basin_gauges(target: dict, ups: list[dict], net: Network, inv_q: pd.DataFrame, target_area_km2: float, now: pd.Timestamp) -> list[Gauge]:
    """Candidate upstream gauges for one basin, with outermost (non-nested) flags set."""
    if not target.get("comid"):  # a few gauges aren't indexed to an NHDPlus flowline
        return []
    t_comid, t_meas = int(target["comid"]), float(target["measure"] or 50.0)
    if t_comid not in net.v.index:
        return []
    t_km, _ = net.position(t_comid, t_meas)
    cands: list[tuple[Gauge, list[int]]] = []
    for u in ups:
        site = u["identifier"].split("-", 1)[1]
        if site == target["identifier"].split("-", 1)[1] or site not in inv_q.index or not u.get("comid"):
            continue
        rec = inv_q.loc[site]
        years = (min(rec["end"], now) - max(rec["begin"], RECORD_SINCE)).days / 365.25
        if years < MIN_RECORD_YEARS or rec["begin"] > MUST_BEGIN_BY:
            continue
        comid, meas = int(u["comid"]), float(u["measure"] if u["measure"] is not None else 50.0)
        if comid not in net.v.index:
            continue
        km, area = net.position(comid, meas)
        if not (MIN_AREA_KM2 <= area < MAX_AREA_FRAC * target_area_km2):
            continue
        p = net.path(comid, t_comid)
        if p is None or (comid == t_comid and meas <= t_meas):
            continue
        g = Gauge(site, comid, meas, area, km - t_km, net.travel_hours(p, meas, t_meas), years)
        cands.append((g, p))
    comids = {}
    for g, _ in cands:
        comids.setdefault(g.comid, []).append(g)
    for g, p in cands:
        downstream = set(p[1:])
        nested = any(c in downstream for c in comids)
        # Another candidate on the same flowline but further downstream also nests this one.
        nested |= any(h is not g and h.measure < g.measure for h in comids[g.comid])
        g.outermost = not nested
    return [g for g, _ in cands]


def slots(gauges: list[Gauge], n: int = N_SLOTS) -> list[Gauge]:
    """The `n` largest outermost gauges (by drainage area)."""
    return sorted((g for g in gauges if g.outermost), key=lambda g: -g.area_km2)[:n]


def discover(sites: list[str], cache: Path, net: Network, inv_q: pd.DataFrame, areas: pd.Series) -> dict[str, list[dict]]:
    now = pd.Timestamp.now(tz="UTC")
    out = {}
    for i, s in enumerate(sites, 1):
        feat = site_feature(cache, s)
        ups = upstream_sites(cache, s) if feat else []
        gs = basin_gauges(feat, ups, net, inv_q, float(areas[s]), now) if feat else []
        out[s] = [asdict(g) for g in gs]
        if i % 50 == 0:
            log.info("upstream discovery %d/%d", i, len(sites))
    return out
