"""Public operational benchmarks for multi-basin validation-year comparisons (WY2021-2022).

* NWM v2.0/v2.1 operational forecasts from the Google Cloud archive (`gs://national-water-model`, public, free to
  read). Archived channel_rt files are ~14 MB, but `streamflow` is three shuffle+deflate chunks of ~0.65 MB: the
  HDF5 metadata is parsed from a cached head of the file and only the streamflow chunks are fetched, so one file
  costs ~2 MB for any number of reaches. The reach set changed with v2.1 (2021-04-20 12Z): 2,729,077 reaches
  before, 2,776,738 after, so positions are looked up per reach count.
* NWS river forecasts (every RFC) from the Iowa Environmental Mesonet's processed HML archive (2012+): one
  request per station and UTC year; flow comes from the RFC's own rating (kcfs), so no rating conversion.
* USGS -> NWS location id from the HADS crosswalk, and flood categories from the NWPS gauge API.

`pair_bulletins` pairs RFC bulletins with a model's cycle issues without exact-hour joins: a bulletin issued at
t_r is paired with the latest cycle issue T <= t_r (so the model never knows more than the forecaster), and the
bulletin's trace is linearly interpolated to the model's valid times T + L that fall after t_r and inside the trace.
"""

from __future__ import annotations

import io
import logging
import re
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import h5py
import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

GCS_NWM = "https://storage.googleapis.com/national-water-model"
IEM_HML = "https://mesonet.agron.iastate.edu/cgi-bin/request/hml.py"
NWPS_GAUGE = "https://api.water.noaa.gov/nwps/v1/gauges/{}"
HADS_CROSSWALK = "https://hads.ncep.noaa.gov/USGS/ALL_USGS-HADS_SITES.txt"
HEAD_BYTES = 1 << 19
FLOW_UNITS = {"kcfs": 1000.0, "cfs": 1.0}


# ------------------------------------------------------------------ NWM archive on Google Cloud


def gcs_medium_range_url(day: str, cycle: int, member: int, lead: int) -> str:
    return f"{GCS_NWM}/nwm.{day}/medium_range_mem{member}/nwm.t{cycle:02d}z.medium_range.channel_rt_{member}.f{lead:03d}.conus.nc"


def gcs_short_range_url(day: str, cycle: int, lead: int) -> str:
    return f"{GCS_NWM}/nwm.{day}/short_range/nwm.t{cycle:02d}z.short_range.channel_rt.f{lead:03d}.conus.nc"


class RangeFile(io.RawIOBase):
    """Read-only file object over HTTP: the first `head` bytes are fetched once, anything else by range request."""

    def __init__(self, url: str, session: requests.Session, head: int = HEAD_BYTES):
        self.url, self.session, self.pos = url, session, 0
        r = self.get(0, head - 1)
        self.size = int(r.headers["Content-Range"].split("/")[1])
        self.head = r.content

    def get(self, first: int, last: int) -> requests.Response:
        for attempt in range(5):
            try:
                r = self.session.get(self.url, headers={"Range": f"bytes={first}-{last}"}, timeout=60)
                if r.status_code == 404:
                    raise FileNotFoundError(self.url)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
        raise RuntimeError("unreachable")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else (self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def tell(self) -> int:
        return self.pos

    def readinto(self, buf) -> int:
        first, last = self.pos, min(self.pos + len(buf), self.size)
        data = self.head[first:last] if last <= len(self.head) else self.get(first, last - 1).content
        buf[: len(data)] = data
        self.pos += len(data)
        return len(data)


def _unshuffle_int32(raw: bytes, n: int) -> np.ndarray:
    data = np.frombuffer(zlib.decompress(raw), np.uint8)
    return np.frombuffer(data.reshape(4, -1).T.tobytes(), "<i4")[:n]


def read_reaches(url: str, positions: dict[int, np.ndarray] | np.ndarray, session: requests.Session, var: str = "streamflow") -> np.ndarray:
    """Values (m3/s; NaN where missing) of `var` at reach positions of one channel_rt file. `positions` may be
    {number of reaches in the file: positions}, for archives spanning NWM versions with different reach sets."""
    f = RangeFile(url, session)
    with h5py.File(f, "r") as h:
        ds = h[var]
        idx = positions[ds.shape[0]] if isinstance(positions, dict) else np.asarray(positions)
        chunk = ds.chunks[0]
        scale = float(ds.attrs["scale_factor"][0])
        fill = int(ds.attrs["_FillValue"][0])
        out = np.full(len(idx), np.nan)
        for c in np.unique(idx // chunk):
            info = ds.id.get_chunk_info_by_coord((int(c * chunk),))
            raw = f.get(info.byte_offset, info.byte_offset + info.size - 1).content
            values = _unshuffle_int32(raw, min(chunk, ds.shape[0] - c * chunk))
            sel = idx // chunk == c
            v = values[idx[sel] - c * chunk].astype(np.float64)
            v[v == fill] = np.nan
            out[sel] = v * scale
    return out


def reach_positions(url: str, reaches: list[int], session: requests.Session) -> np.ndarray:
    """Positions of NWM feature_ids `reaches` in one file's reach order."""
    with h5py.File(RangeFile(url, session), "r") as h:
        fid = h["feature_id"][:]
    return pd.Series(np.arange(len(fid)), index=fid).loc[reaches].to_numpy()


# ------------------------------------------------------------------ NWS forecasts (IEM HML archive)


def hads_crosswalk(text: str) -> pd.DataFrame:
    """USGS site number -> NWS location id from the HADS `ALL_USGS-HADS_SITES.txt` listing."""
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 7 and re.fullmatch(r"[A-Z0-9]{5}", parts[0].strip()) and parts[1].strip().isdigit():
            rows.append((parts[1].strip(), parts[0].strip(), parts[3].strip()))
    return pd.DataFrame(rows, columns=["usgs", "lid", "hsa"]).drop_duplicates("usgs")


def nwps_gauge(lid: str, session: requests.Session | None = None) -> dict:
    """RFC, WFO, forecast PEDTS and flood-category stages and flows of one NWS gauge (empty dict if unknown)."""
    r = (session or requests).get(NWPS_GAUGE.format(lid), timeout=60)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    d = r.json()
    cats = (d.get("flood") or {}).get("categories") or {}
    out = {"lid": lid, "usgs": d.get("usgsId"), "rfc": (d.get("rfc") or {}).get("abbreviation"), "wfo": (d.get("wfo") or {}).get("abbreviation"),
           "pedts_forecast": (d.get("pedts") or {}).get("forecast"), "lat": d.get("latitude"), "lon": d.get("longitude")}
    for c in ("action", "minor", "moderate", "major"):
        for k in ("stage", "flow"):
            v = (cats.get(c) or {}).get(k)
            out[f"{c}_{k}"] = v if isinstance(v, (int, float)) and v > 0 else None
    return out


def hml_forecasts(lid: str, years: list[int], session: requests.Session | None = None) -> pd.DataFrame:
    """Every HML forecast of one station issued in the given UTC years (IEM allows one year per request)."""
    s = session or requests.Session()
    frames = []
    for y in years:
        params = {"station": lid, "sts": f"{y}-01-01T00:00Z", "ets": f"{y + 1}-01-01T00:00Z", "fmt": "csv", "kind": "forecasts", "tz": "UTC"}
        r = s.get(IEM_HML, params=params, timeout=180)
        r.raise_for_status()
        if not r.text.startswith("station,"):
            continue
        f = pd.read_csv(io.StringIO(r.text)).rename(columns={"issued[UTC]": "issued", "forecast_valid[UTC]": "valid"})
        if not f.empty:
            frames.append(f.loc[:, ~f.columns.duplicated()])
    if not frames:
        return pd.DataFrame(columns=["station", "issued", "valid"])
    df = pd.concat(frames, ignore_index=True)
    df["issued"] = pd.to_datetime(df["issued"], utc=True)
    df["valid"] = pd.to_datetime(df["valid"], utc=True)
    return df


def hml_flow(df: pd.DataFrame) -> pd.DataFrame:
    """(issued, valid, flow ft3/s) from HML rows: the flow series where the forecast has one (secondary `Flow`, or a
    primary `Total Discharge`/`Flow`); stage-only and reservoir pool forecasts are dropped."""
    if df.empty:
        return pd.DataFrame(columns=["issued", "valid", "flow"])
    sec = df["secondaryname"].eq("Flow") & df["secondaryunits"].isin(list(FLOW_UNITS))
    pri = df["primaryname"].isin(["Total Discharge", "Flow"]) & df["primaryunits"].isin(list(FLOW_UNITS))
    flow = np.where(sec, df["secondary_value"] * df["secondaryunits"].map(FLOW_UNITS), np.where(pri, df["primary_value"] * df["primaryunits"].map(FLOW_UNITS), np.nan))
    out = pd.DataFrame({"issued": df["issued"], "valid": df["valid"], "flow": flow.astype(float)})
    return out[np.isfinite(out["flow"]) & (out["flow"] >= 0)].sort_values(["issued", "valid"], ignore_index=True)


def pair_bulletins(bulletins: pd.DataFrame, cycle_leads: dict[pd.Timestamp, np.ndarray], cycle_hours: int = 6) -> pd.DataFrame:
    """Pair RFC bulletins (issued, valid, flow) with a model's cycle issues.

    `cycle_leads` maps each model issue time (on the `cycle_hours` grid) to its available leads (h). Returns one row
    per (bulletin, model lead): issued, issue_time (T, the latest cycle at or before the bulletin), lead_h,
    valid (T + lead_h), rfc_lead_h (valid - issued) and the bulletin's flow interpolated to `valid`. Valid times
    before the bulletin or outside its trace are skipped; nothing is joined on exact hours."""
    rows = []
    for t_r, tr in bulletins.groupby("issued"):
        T = t_r.floor(f"{cycle_hours}h")
        leads = cycle_leads.get(T)
        if leads is None or tr.empty:
            continue
        tv = tr["valid"].to_numpy("datetime64[ns]").astype(np.int64).astype(np.float64)
        valid = pd.DatetimeIndex(T + pd.to_timedelta(np.asarray(leads, float), unit="h"))
        vi = valid.as_unit("ns").asi8.astype(np.float64)
        ok = (valid > t_r) & (vi >= tv.min()) & (vi <= tv.max())
        if ok.any():
            rows.append(pd.DataFrame({"issued": t_r, "issue_time": T, "lead_h": np.asarray(leads, float)[ok], "valid": valid[ok],
                                      "flow": np.interp(vi[ok], tv, tr["flow"].to_numpy(np.float64))}))
    if not rows:
        return pd.DataFrame(columns=["issued", "issue_time", "lead_h", "valid", "rfc_lead_h", "flow"])
    out = pd.concat(rows, ignore_index=True)
    out["rfc_lead_h"] = (out["valid"] - out["issued"]).dt.total_seconds() / 3600
    return out


# ------------------------------------------------------------------ bulk pulls (validation years)

MR_CYCLES = (0, 12)
# multiples of 3 (v2.0 medium-range output is 3-hourly), covering the scored leads and the same leads + 6 h
MR_LEADS = (3, 6, 9, 12, 15, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72, 78, 84, 90, 96, 102, 108, 114, 120, 126, 132, 138, 144, 150, 156, 162, 168, 174)
SR_CYCLES = (0, 6, 12, 18)
SR_LEADS = (1, 2, 3, 6, 9, 12, 18)
# the cycle 2 h before each issue, at the issue's leads + 2 h (what a user has at issue time)
SR_PREV_CYCLES = (4, 10, 16, 22)
SR_PREV_LEADS = (3, 4, 5, 8, 11, 14)
NWM_V20_PROBE, NWM_V21_PROBE = "20201001", "20220915"


def nwm_day_urls(day: str) -> list[str]:
    return ([gcs_medium_range_url(day, c, k, lead) for c in MR_CYCLES for k in range(1, 8) for lead in MR_LEADS]
            + [gcs_short_range_url(day, c, lead) for c in SR_CYCLES for lead in SR_LEADS]
            + [gcs_short_range_url(day, c, lead) for c in SR_PREV_CYCLES for lead in SR_PREV_LEADS])


def nwm_positions(reaches: list[int], session: requests.Session) -> dict[int, np.ndarray]:
    """{number of reaches: positions of `reaches`} for the v2.0 and v2.1 reach sets of the GCS archive."""
    out = {}
    for day in (NWM_V20_PROBE, NWM_V21_PROBE):
        url = gcs_medium_range_url(day, 0, 1, 24)
        with h5py.File(RangeFile(url, session), "r") as h:
            fid = h["feature_id"][:]
        out[len(fid)] = pd.Series(np.arange(len(fid)), index=fid).loc[reaches].to_numpy()
    return out


def nwm_day(day: str, positions: dict[int, np.ndarray], threads: int = 8) -> dict[str, np.ndarray]:
    """One day of the validation-year NWM pull (m3/s, NaN where a file is missing):
    mr [cycle, member 1-7, MR_LEADS, reach], sr [cycle, SR_LEADS, reach], sr_prev [cycle, SR_PREV_LEADS, reach]."""
    n = len(next(iter(positions.values())))
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=threads))

    def one(url: str) -> np.ndarray:
        try:
            return read_reaches(url, positions, session).astype(np.float32)
        except FileNotFoundError:
            return np.full(n, np.nan, np.float32)

    with ThreadPoolExecutor(threads) as ex:
        values = np.stack(list(ex.map(one, nwm_day_urls(day))))
    n_mr, n_sr = len(MR_CYCLES) * 7 * len(MR_LEADS), len(SR_CYCLES) * len(SR_LEADS)
    return {"mr": values[:n_mr].reshape(len(MR_CYCLES), 7, len(MR_LEADS), n),
            "sr": values[n_mr : n_mr + n_sr].reshape(len(SR_CYCLES), len(SR_LEADS), n),
            "sr_prev": values[n_mr + n_sr :].reshape(len(SR_PREV_CYCLES), len(SR_PREV_LEADS), n)}
