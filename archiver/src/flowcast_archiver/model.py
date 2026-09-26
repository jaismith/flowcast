from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import requests


@dataclass
class RawPayload:
    url: str
    fetched_at: datetime
    status: int
    content_type: str
    body: bytes

    @classmethod
    def from_response(cls, resp: requests.Response, fetched_at: datetime) -> RawPayload:
        return cls(
            url=resp.url,
            fetched_at=fetched_at,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", ""),
            body=resp.content,
        )

    def to_record(self, dataset: str, key: str) -> dict:
        record = {
            "dataset": dataset,
            "key": key,
            "url": self.url,
            "fetched_at": self.fetched_at.isoformat(),
            "status": self.status,
            "content_type": self.content_type,
        }
        try:
            record["body"] = self.body.decode("utf-8")
        except UnicodeDecodeError:
            record["body_b64"] = base64.b64encode(self.body).decode("ascii")
        return record


@dataclass
class Issuance:
    """One forecast as issued: its raw payload(s) plus normalized rows."""

    dataset: str
    key: str  # unique within the dataset, e.g. "CCRN6/2026-09-25T19:16:00Z"
    issue_time: datetime
    frame: pd.DataFrame | None  # None for raw-only datasets
    raw: list[RawPayload] = field(default_factory=list)
