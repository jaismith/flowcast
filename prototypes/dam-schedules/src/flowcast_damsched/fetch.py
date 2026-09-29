"""Polite HTTP for schedule sources: robots.txt, a per-host delay, and raw payloads kept on disk.

Every parser works on the raw bytes saved here, so a parser fix can be replayed over past fetches
without refetching (and the archive keeps what was published at the time, which no source does).
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

USER_AGENT = "Mozilla/5.0 (compatible; flowcast-research/0.1; non-commercial river forecasting)"
MIN_INTERVAL_S = 2.0


class RobotsDisallowed(RuntimeError):
    pass


@dataclass(frozen=True)
class Fetched:
    url: str
    body: bytes
    fetched_at: datetime
    status: int
    content_type: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class Fetcher:
    def __init__(self, raw_dir: Path | None = None, min_interval_s: float = MIN_INTERVAL_S, timeout: float = 60.0):
        self.raw_dir = raw_dir
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(f"{host}/robots.txt", timeout=self.timeout)
                # A missing robots.txt allows everything; an error page that isn't text is treated as missing.
                rp.parse(resp.text.splitlines() if resp.ok and "text" in resp.headers.get("content-type", "") else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def get(self, url: str, **params) -> Fetched:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        host = urlsplit(url).netloc
        wait = self._last.get(host, 0.0) + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, params=params or None, timeout=self.timeout)
        self._last[host] = time.monotonic()
        resp.raise_for_status()
        got = Fetched(url, resp.content, datetime.now(UTC), resp.status_code, resp.headers.get("content-type", ""))
        if self.raw_dir is not None:
            self._save(got)
        return got

    def _save(self, got: Fetched) -> None:
        parts = urlsplit(got.url)
        name = (parts.path.strip("/").replace("/", "_") or "index")[-120:]
        day = got.fetched_at.strftime("%Y-%m-%d")
        path = self.raw_dir / parts.netloc / day / f"{got.fetched_at:%H%M%S}_{got.sha256[:12]}_{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(got.body)
