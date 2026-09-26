"""GEFSv12 reforecast (2000-2019) extracted to basins and elevation bands, in the operational GEFS vocabulary.

Source: `s3://noaa-gefs-retrospective/GEFSv12/reforecast/<year>/<YYYYMMDD00>/<member>/Days:1-10/`, one GRIB2 file
per variable, member and 00Z init, 0.25 degree, 3-hourly leads 3-240 h, with `.idx` byte offsets. Members
c00 and p01-p04 run every day (p05-p10 only on Wednesdays, not used, so every init has the same five).

Per (init, member): for each variable, one ranged GET covers leads 3-189 h (the first 63 messages, 126 for the
interleaved 10 m / 100 m wind files). Only the grid cells our basins touch are kept after decoding.

Matching the cube's `gefs_*` arrays (dynamical.org operational GEFS):

* the same seven outputs and units, and the same lead grid (0-189 h every 3 h). The reforecast has no 0 h
  step, so lead 0 is NaN for every variable (operational has instantaneous values there);
* precipitation and radiation are 0-3 h / 0-6 h bucket accumulations or averages in the files; they are
  converted to the mean rate over the 3 h ending at each lead, which is how the operational arrays are stored;
* there is no 2 m relative humidity, so dewpoint comes from 2 m specific humidity and surface pressure
  (as for AORC); operational GEFS derives it from temperature and relative humidity.
"""

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import pyproj  # noqa: F401  # isort: skip  (must load before eccodes: their bundled native libraries clash at exit otherwise)
import eccodes  # isort: skip
import numpy as np
import pandas as pd
import requests
import scipy.sparse as sp

from .extract import write_output
from .grids import GEFS, GEOGRAPHIC, Grid
from .sources import HRRR_OUT, dewpoint_from_q

log = logging.getLogger(__name__)

BASE_URL = "https://noaa-gefs-retrospective.s3.amazonaws.com/GEFSv12/reforecast"
MEMBERS = ("c00", "p01", "p02", "p03", "p04")
N_MESSAGES = 63  # leads 3..189 h
LEADS_H = np.arange(0, 190, 3, dtype=np.float32)  # 64 leads, index 0 = 0 h
FIRST_INIT = pd.Timestamp("2000-01-01")
LAST_INIT = pd.Timestamp("2019-12-31")
# (file variable, level filter, kind): kind is "instant", "accum" (bucket sum) or "avg" (bucket mean).
FILES = {
    "apcp_sfc": (None, "accum"),
    "tmp_2m": (None, "instant"),
    "spfh_2m": (None, "instant"),
    "pres_sfc": (None, "instant"),
    "ugrd_hgt": (10, "instant"),
    "vgrd_hgt": (10, "instant"),
    "dswrf_sfc": (None, "avg"),
    "dlwrf_sfc": (None, "avg"),
}

# Reforecast files run 0-359.75 E; a grid starting at -360 puts CONUS longitudes at the same indices.
RF_GRID = Grid("gefs_reforecast", GEOGRAPHIC.to_wkt(), -360.0, 0.25, 1440, 90.0, -0.25, 721, 721, 1440)


def inits() -> pd.DatetimeIndex:
    return pd.date_range(FIRST_INIT, LAST_INIT, freq="D")


def month_shards() -> list[str]:
    return sorted({f"{t:%Y-%m}" for t in inits()})


def aggregate_to_grid(w_aorc: sp.csr_matrix, aorc: Grid, target: Grid) -> sp.csr_matrix:
    """Re-express AORC-cell weights on a coarser target grid by summing the AORC cells inside each target cell."""
    cols = np.unique(w_aorc.indices)
    iy, ix = np.divmod(cols, aorc.nx)
    lat, lon = aorc.y0 + aorc.dy * iy, aorc.x0 + aorc.dx * ix
    ty = np.floor((lat - target.y0) / target.dy + 0.5).astype(int)
    tx = np.floor((lon - target.x0) / target.dx + 0.5).astype(int)
    m = sp.csr_matrix((np.ones(len(cols)), (cols, ty * target.nx + tx)), shape=(w_aorc.shape[1], target.ny * target.nx))
    return (w_aorc @ m).tocsr()


def gefs_band_weights(w_aorc_all: sp.csr_matrix, n_basins: int, aorc: Grid, target: Grid = GEFS) -> sp.csr_matrix:
    """Elevation-band rows (basin*bands + band) on a GEFS grid."""
    return aggregate_to_grid(w_aorc_all[n_basins:], aorc, target)


@dataclass
class RFPlan:
    units: list[str]
    cells: np.ndarray  # flat RF_GRID indices with any weight
    weights_t: sp.csr_matrix  # (units, len(cells))
    wsum: np.ndarray


def make_plan(units: list[str], w: sp.csr_matrix) -> RFPlan:
    cells = np.unique(w.indices)
    sub = w[:, cells].tocsr().astype(np.float32)
    return RFPlan(units, cells, sub, np.asarray(sub.sum(axis=1)).ravel().astype(np.float32))


# ----------------------------------------------------------------------------------------- reading

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.mount("https://", requests.adapters.HTTPAdapter(max_retries=requests.adapters.Retry(total=6, backoff_factor=1.0, status_forcelist=(500, 502, 503, 504))))
    return _SESSION


def _url(init: pd.Timestamp, member: str, var: str) -> str:
    stamp = f"{init:%Y%m%d}00"
    return f"{BASE_URL}/{init:%Y}/{stamp}/{member}/Days:1-10/{var}_{stamp}_{member}.grib2"


def fetch_messages(init: pd.Timestamp, member: str, var: str) -> bytes | None:
    url = _url(init, member, var)
    idx = _session().get(url + ".idx", timeout=60)
    if idx.status_code == 404:
        return None
    idx.raise_for_status()
    offsets = [int(line.split(":")[1]) for line in idx.text.strip().splitlines()]
    n = 2 * N_MESSAGES if FILES[var][0] is not None else N_MESSAGES
    end = offsets[n] - 1 if len(offsets) > n else ""
    resp = _session().get(url, headers={"Range": f"bytes=0-{end}"}, timeout=300)
    resp.raise_for_status()
    return resp.content


def decode(data: bytes, cells: np.ndarray, level: int | None) -> dict[tuple[int, int], np.ndarray]:
    """{(startStep, endStep): values at `cells`} for every message (optionally one level only)."""
    out = {}
    view = memoryview(data)
    pos = 0
    while pos < len(data):
        length = int.from_bytes(data[pos + 8 : pos + 16], "big")  # GRIB2 section 0, octets 9-16
        h = eccodes.codes_new_from_message(view[pos : pos + length])
        pos += length
        try:
            if level is not None and eccodes.codes_get(h, "level") != level:
                continue
            key = (eccodes.codes_get(h, "startStep"), eccodes.codes_get(h, "endStep"))
            out[key] = eccodes.codes_get_values(h)[cells].astype(np.float32)
        finally:
            eccodes.codes_release(h)
    return out


def to_leads(fields: dict[tuple[int, int], np.ndarray], kind: str, n_cells: int) -> np.ndarray:
    """(64 leads, cells): instant values, or mean rates over the 3 h ending at each lead."""
    out = np.full((len(LEADS_H), n_cells), np.nan, dtype=np.float32)
    by_end = {e: (s, v) for (s, e), v in fields.items()}
    for k, lead in enumerate(LEADS_H.astype(int)):
        if lead == 0 or lead not in by_end:
            continue
        s, v = by_end[lead]
        if kind == "instant":
            out[k] = v
            continue
        prev = fields.get((s, lead - 3)) if lead - 3 > s else None
        if kind == "accum":
            total = v - prev if prev is not None else v
            out[k] = total / 3.0  # mm over 3 h -> mm/h
        else:
            out[k] = (v * (lead - s) - prev * (lead - 3 - s)) / 3.0 if prev is not None else v
    return out


def member_values(init: pd.Timestamp, member: str, plan: RFPlan) -> np.ndarray | None:
    """(64, units, 7) basin/band means for one init and member, or None if the member is missing."""
    raw = {}
    for var, (level, kind) in FILES.items():
        data = fetch_messages(init, member, var)
        if data is None:
            return None
        raw[var] = to_leads(decode(data, plan.cells, level), kind, len(plan.cells))
    with np.errstate(invalid="ignore"):
        conv = {
            "precip_mm_h": np.maximum(raw["apcp_sfc"], 0.0),
            "temp_2m_c": raw["tmp_2m"] - 273.15,
            "dewpoint_2m_c": dewpoint_from_q(raw["spfh_2m"], raw["pres_sfc"]),
            "pressure_kpa": raw["pres_sfc"] / 1000.0,
            "wind_speed_10m": np.hypot(raw["ugrd_hgt"], raw["vgrd_hgt"]),
            "sw_down_wm2": np.maximum(raw["dswrf_sfc"], 0.0),
            "lw_down_wm2": raw["dlwrf_sfc"],
        }
    out = np.empty((len(LEADS_H), len(plan.units), len(HRRR_OUT)), dtype=np.float32)
    for k, name in enumerate(HRRR_OUT):
        x = conv[name]
        out[:, :, k] = np.asarray(plan.weights_t @ x.T).T / plan.wsum
    return out


# ------------------------------------------------------------------------------------------ worker

_PLAN: RFPlan | None = None


def _init(plan_path: str) -> None:
    global _PLAN
    _PLAN = pd.read_pickle(plan_path)


def _task(init: pd.Timestamp, m: int) -> tuple[pd.Timestamp, int, np.ndarray | None]:
    return init, m, member_values(init, MEMBERS[m], _PLAN)


def run_shards(shards: list[str], plan_path: Path, out_dir: Path, workers: int, upload=None) -> None:
    """Month shards -> `<out_dir>/gefs_reforecast/<YYYY-MM>/all.npy.zst` (units, inits, members, leads, vars)."""
    plan: RFPlan = pd.read_pickle(plan_path)
    all_inits = inits()
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(str(plan_path),)) as pool:
        for shard in shards:
            month = all_inits[all_inits.strftime("%Y-%m") == shard]
            values = np.full((len(plan.units), len(month), len(MEMBERS), len(LEADS_H), len(HRRR_OUT)), np.nan, dtype=np.float32)
            t0 = time.time()
            pending = {pool.submit(_task, t, m) for t in month for m in range(len(MEMBERS))}
            missing = 0
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    t, m, v = fut.result()
                    if v is None:
                        missing += 1
                        continue
                    values[:, month.get_loc(t), m] = np.moveaxis(v, 1, 0)
            meta = {
                "source": "gefs_reforecast",
                "shard": shard,
                "phase": "B",
                "units": plan.units,
                "variables": list(HRRR_OUT),
                "leading": [str(t) for t in month],
                "leads_h": LEADS_H.tolist(),
                "members": len(MEMBERS),
                "missing_members": missing,
            }
            path = out_dir / "gefs_reforecast" / shard / "all.npy.zst"
            write_output(path, values, meta)
            if upload:
                upload(path)
                upload(path.with_suffix("").with_suffix(".json"))
            log.info("reforecast %s: %d inits, %d missing members, %.1f min", shard, len(month), missing, (time.time() - t0) / 60)
