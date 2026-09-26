"""Client for the USGS Water Data APIs (OGC API - Features + STAC), replacing legacy NWIS web services.

Legacy `waterservices.usgs.gov` is throttled from Nov 16, 2026 and shut down Feb 22, 2027. This client
only talks to `api.waterdata.usgs.gov`.

Docs: https://api.waterdata.usgs.gov/docs/ogcapi/
"""

import functools
import json
import logging
import os
import random
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import boto3
import pandas as pd
import requests

from .cache import ResponseCache
from .params import Parameter, Statistic, site_id, site_number
from .ratings import RatingCurve

log = logging.getLogger(__name__)

OGC_BASE = "https://api.waterdata.usgs.gov/ogcapi/v0"
STAC_BASE = "https://api.waterdata.usgs.gov/stac/v0"
API_HOST = "api.waterdata.usgs.gov"
# api.data.gov key: taken from API_KEY_ENV, else read from the SSM SecureString named by API_KEY_PARAMETER_ENV
# (how the Lambdas get it), else requests are anonymous.
API_KEY_ENV = "API_DATA_GOV_KEY"
API_KEY_PARAMETER_ENV = "API_DATA_GOV_KEY_PARAMETER"
RETRY_STATUS = {429, 500, 502, 503, 504}

CONTINUOUS_COLUMNS = ["time", "value", "approval_status", "qualifier", "time_series_id"]
DAILY_COLUMNS = ["date", "value", "approval_status", "qualifier", "time_series_id"]


class WaterDataError(RuntimeError):
    pass


def _utc(ts: datetime | date | str) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_api_key() -> str | None:
    if key := os.environ.get(API_KEY_ENV):
        return key
    parameter = os.environ.get(API_KEY_PARAMETER_ENV)
    return _ssm_api_key(parameter) if parameter else None


@functools.cache
def _ssm_api_key(parameter: str) -> str | None:
    """Cached per process so a warm Lambda reads the parameter once. Never logs the value."""
    try:
        key = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    except Exception as exc:
        log.warning("USGS API key parameter %s unreadable (%s); using anonymous requests", parameter, type(exc).__name__)
        return None
    log.info("USGS API key loaded from %s", parameter)
    return key or None


class WaterDataClient:
    """Fetches discharge, water temperature, stage, site metadata and ratings.

    Historical requests are split into calendar-year chunks (continuous) or decade chunks (daily)
    and cached on disk. Pass `use_cache=False` for small live windows such as the hourly ingest.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache: ResponseCache | None = None,
        session: requests.Session | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 5,
        backoff_s: float = 2.0,
        min_interval_s: float = 0.0,
        page_size: int = 50_000,
        live_ttl: timedelta = timedelta(minutes=15),
        provisional_ttl: timedelta = timedelta(days=1),
    ):
        self.api_key = api_key if api_key is not None else resolve_api_key()
        self.cache = cache if cache is not None else ResponseCache()
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.min_interval_s = min_interval_s
        self.page_size = page_size
        self.live_ttl = live_ttl
        self.provisional_ttl = provisional_ttl
        self._last_request = 0.0

    # ------------------------------------------------------------------ HTTP

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        headers = {"User-Agent": "flowcast/0.1 (+https://github.com/jaismith/flowcast)"}
        # Header rather than `api_key` so the key stays out of URLs (errors, `next` links); only sent to the
        # USGS host because rating asset hrefs are followed as given.
        if self.api_key and urlsplit(url).hostname == API_HOST:
            headers["X-Api-Key"] = self.api_key
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.max_retries:
                    raise WaterDataError(f"GET {url} failed: {exc}") from exc
                self._sleep_backoff(attempt, None)
                continue
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and int(remaining) < 10:
                log.warning("USGS API rate limit nearly exhausted (%s left)", remaining)
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code >= 400:
                raise WaterDataError(f"GET {resp.url} -> {resp.status_code}: {resp.text[:500]}")
            return resp
        raise AssertionError("unreachable")

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = self.backoff_s * 2**attempt * (0.5 + random.random())
        log.info("USGS API retry %d in %.1fs", attempt + 1, delay)
        time.sleep(delay)

    def _features(self, collection: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """All feature property dicts for a query, following `next` links."""
        url: str | None = f"{OGC_BASE}/collections/{collection}/items"
        query: dict[str, Any] | None = {"f": "json", "limit": self.page_size, "skipGeometry": "true", **params}
        out: list[dict[str, Any]] = []
        while url:
            payload = self._get(url, query).json()
            out.extend(f["properties"] | {"id": f.get("id")} for f in payload.get("features", []))
            url = next((link["href"] for link in payload.get("links", []) if link.get("rel") == "next"), None)
            query = None  # the next link already carries the query string
        return out

    # ---------------------------------------------------------- time series

    def continuous(
        self,
        site: str,
        parameter: Parameter | str,
        start: datetime | date | str,
        end: datetime | date | str,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Instantaneous values (typically 15-min) in [start, end], `time` as tz-aware UTC."""
        sid, param = site_id(site), Parameter(parameter)
        start_ts, end_ts = _utc(start), _utc(end)
        if not use_cache:
            return self._fetch_continuous(sid, param, start_ts, end_ts)
        frames = []
        for year in range(start_ts.year, end_ts.year + 1):
            chunk_start = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
            chunk_end = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC") - pd.Timedelta(seconds=1)
            key = f"continuous/{sid}/{param}/{year}"
            frames.append(
                self._cached_chunk(key, chunk_end, lambda s=chunk_start, e=chunk_end: self._fetch_continuous(sid, param, s, e))
            )
        df = pd.concat(frames, ignore_index=True) if frames else _empty(CONTINUOUS_COLUMNS)
        return df[(df["time"] >= start_ts) & (df["time"] <= end_ts)].reset_index(drop=True)

    def daily(
        self,
        site: str,
        parameter: Parameter | str,
        start: date | str,
        end: date | str,
        statistic: Statistic | str = Statistic.MEAN,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Daily values (local-day statistics) in [start, end], `date` as a naive datetime64."""
        sid, param, stat = site_id(site), Parameter(parameter), Statistic(statistic)
        start_d, end_d = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        if not use_cache:
            return self._fetch_daily(sid, param, stat, start_d, end_d)
        frames = []
        for decade in range(start_d.year // 10 * 10, end_d.year + 1, 10):
            chunk_start, chunk_end = pd.Timestamp(year=decade, month=1, day=1), pd.Timestamp(year=decade + 9, month=12, day=31)
            key = f"daily/{sid}/{param}/{stat}/{decade}s"
            frames.append(
                self._cached_chunk(
                    key,
                    chunk_end.tz_localize("UTC") + pd.Timedelta(days=2),
                    lambda s=chunk_start, e=chunk_end: self._fetch_daily(sid, param, stat, s, e),
                )
            )
        df = pd.concat(frames, ignore_index=True) if frames else _empty(DAILY_COLUMNS)
        return df[(df["date"] >= start_d) & (df["date"] <= end_d)].reset_index(drop=True)

    def continuous_many(
        self,
        sites: list[str],
        parameter: Parameter | str,
        start: datetime | date | str,
        end: datetime | date | str,
    ) -> pd.DataFrame:
        """Uncached instantaneous values for many sites in one query (for short live windows).

        Returns `monitoring_location_id` plus the `continuous()` columns; sites without the parameter are absent.
        """
        start_ts, end_ts = _utc(start), min(_utc(end), pd.Timestamp.now(tz="UTC"))
        rows = self._features(
            "continuous",
            {
                "monitoring_location_id": ",".join(site_id(s) for s in sites),
                "parameter_code": Parameter(parameter).value,
                "time": f"{_iso(start_ts)}/{_iso(end_ts)}",
                "properties": "monitoring_location_id,time,value,approval_status,qualifier,time_series_id",
            },
        )
        columns = ["monitoring_location_id", *CONTINUOUS_COLUMNS]
        if not rows:
            return _empty(columns)
        df = _series_frame(rows, "time", columns)
        return df.sort_values(["monitoring_location_id", "time"], kind="stable").reset_index(drop=True)

    def latest_continuous(self, sites: list[str], parameter: Parameter | str) -> pd.DataFrame:
        """Most recent instantaneous value per site (one request for many sites)."""
        rows = self._features(
            "latest-continuous",
            {"monitoring_location_id": ",".join(site_id(s) for s in sites), "parameter_code": Parameter(parameter).value},
        )
        df = pd.DataFrame(rows)
        if df.empty:
            return _empty(["monitoring_location_id", *CONTINUOUS_COLUMNS])
        df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df[["monitoring_location_id", *CONTINUOUS_COLUMNS]]

    def _cached_chunk(self, key: str, chunk_end: pd.Timestamp, fetch) -> pd.DataFrame:
        now = pd.Timestamp.now(tz="UTC")
        ttl = self.live_ttl if chunk_end >= now - pd.Timedelta(days=1) else self.provisional_ttl
        cached = self.cache.get_frame(key, ttl)
        if cached is not None:
            return cached
        frame = fetch()
        immutable = chunk_end < now - pd.Timedelta(days=1) and bool((frame["approval_status"] == "Approved").all())
        self.cache.put_frame(key, frame, immutable=immutable)
        return frame

    def _fetch_continuous(self, sid: str, param: Parameter, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        now = pd.Timestamp.now(tz="UTC")
        if start > now:
            return _empty(CONTINUOUS_COLUMNS)
        rows = self._features(
            "continuous",
            {
                "monitoring_location_id": sid,
                "parameter_code": param.value,
                "time": f"{_iso(start)}/{_iso(min(end, now))}",
                "properties": "time,value,approval_status,qualifier,time_series_id",
            },
        )
        return _series_frame(rows, "time", CONTINUOUS_COLUMNS)

    def _fetch_daily(self, sid: str, param: Parameter, stat: Statistic, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        today = pd.Timestamp.now().normalize()
        if start > today:
            return _empty(DAILY_COLUMNS)
        rows = self._features(
            "daily",
            {
                "monitoring_location_id": sid,
                "parameter_code": param.value,
                "statistic_id": stat.value,
                "time": f"{start:%Y-%m-%d}/{min(end, today):%Y-%m-%d}",
                "properties": "time,value,approval_status,qualifier,time_series_id",
            },
        )
        return _series_frame(rows, "date", DAILY_COLUMNS)

    # ------------------------------------------------------------- metadata

    def monitoring_location(self, site: str) -> dict[str, Any]:
        sid = site_id(site)
        key = f"meta/monitoring-locations/{sid}.json"
        text = self.cache.get_text(key, timedelta(days=7))
        if text is None:
            payload = self._get(f"{OGC_BASE}/collections/monitoring-locations/items/{sid}", {"f": "json"}).json()
            record = payload["properties"] | {"id": payload.get("id", sid), "geometry": payload.get("geometry")}
            text = json.dumps(record)
            self.cache.put_text(key, text)
        return json.loads(text)

    def time_series_metadata(self, site: str, parameter: Parameter | str | None = None) -> pd.DataFrame:
        params: dict[str, Any] = {"monitoring_location_id": site_id(site)}
        if parameter is not None:
            params["parameter_code"] = Parameter(parameter).value
        df = pd.DataFrame(self._features("time-series-metadata", params))
        for col in ("begin", "end"):
            if col in df:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        return df

    def rating_rdb(self, site: str, kind: str = "exsa", ttl: timedelta = timedelta(days=1)) -> str:
        """Raw NWIS RDB text of the current rating (see `rating()`), for callers that archive it."""
        num = site_number(site)
        key = f"ratings/USGS.{num}.{kind}.rdb"
        text = self.cache.get_text(key, ttl)
        if text is None:
            item = self._get(f"{STAC_BASE}/collections/ratings/items/USGS-{num}.{kind}.rdb").json()
            text = self._get(item["assets"]["data"]["href"]).text
            self.cache.put_text(key, text)
        return text

    def rating(self, site: str, kind: str = "exsa") -> RatingCurve:
        """Current stage-discharge rating. `kind` is `exsa` (expanded, shift-adjusted), `base` or `corr`."""
        return RatingCurve.from_rdb(site_id(site), kind, self.rating_rdb(site, kind))


def _empty(columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
    df["value"] = df["value"].astype(float)
    if "time" in df:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _series_frame(rows: list[dict[str, Any]], time_col: str, columns: list[str]) -> pd.DataFrame:
    if not rows:
        return _empty(columns)
    df = pd.DataFrame(rows).rename(columns={"time": time_col})
    for col in columns:
        if col not in df:
            df[col] = None
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    # Some readings carry fractional seconds, so the format can't be inferred from the first row.
    df[time_col] = pd.to_datetime(df[time_col], utc=True, format="ISO8601") if time_col == "time" else pd.to_datetime(df[time_col])
    return df[columns].sort_values(time_col, kind="stable").reset_index(drop=True)
