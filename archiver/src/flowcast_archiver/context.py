from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .config import Config
from .model import RawPayload
from .store import State, utcnow

log = logging.getLogger(__name__)


@dataclass
class Context:
    config: Config
    state: State
    session: requests.Session
    now: datetime
    # Where a source starts when it has no cursor yet (first run = backfill).
    backfill_start: datetime | None = None
    cache: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str, *args) -> None:
        """Record a non-fatal failure; the source moves on to its next item. Fails the run (and alarms)."""
        log.exception(message, *args)
        self.errors.append(message % args)

    def warn(self, message: str, *args) -> None:
        """Note a skipped item that shouldn't page anyone (e.g. one missing file out of dozens)."""
        log.warning(message, *args)
        self.warnings.append(message % args)

    def get(
        self, url: str, params: dict | None = None, timeout: float = 60, headers: dict | None = None
    ) -> tuple[requests.Response, RawPayload]:
        fetched_at = utcnow()
        resp = self.session.get(url, params=params, timeout=timeout, headers=headers)
        resp.raise_for_status()
        return resp, RawPayload.from_response(resp, fetched_at)
