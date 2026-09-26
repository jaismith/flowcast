"""Storage for the archive, addressed by a URI so the backend is configuration.

`s3://bucket/prefix` in AWS, or a local directory (`file:///path` or a plain path).
Layout under the root:

    normalized/{dataset}/month=YYYY-MM/{run_id}.parquet   rows in schema.SCHEMA
    raw/{dataset}/month=YYYY-MM/{run_id}.jsonl.zst        one JSON line per fetched payload
    _state/state.json                                     seen issuance keys and cursors

Data files are write-once; only the state file is rewritten.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import pyarrow.parquet as pq
import zstandard

from .schema import to_table

STATE_KEY = "_state/state.json"
SEEN_RETENTION = timedelta(days=45)
# The AWS SDK for pandas layer's pyarrow is built with snappy and gzip only (no zstd),
# so Parquet uses snappy and raw payloads are zstd-compressed with the zstandard package.
PARQUET_COMPRESSION = "snappy"


@dataclass
class State:
    seen: dict[str, dict[str, str]] = field(default_factory=dict)  # dataset -> key -> issue_time
    cursors: dict[str, str] = field(default_factory=dict)  # dataset -> ISO time of newest issuance

    def has(self, dataset: str, key: str) -> bool:
        return key in self.seen.get(dataset, {})

    def add(self, dataset: str, key: str, issue_time: datetime) -> None:
        self.seen.setdefault(dataset, {})[key] = issue_time.isoformat()
        self.advance_cursor(dataset, issue_time)

    def advance_cursor(self, dataset: str, when: datetime) -> None:
        cursor = self.cursor(dataset)
        if cursor is None or when > cursor:
            self.cursors[dataset] = when.isoformat()

    def cursor(self, dataset: str) -> datetime | None:
        value = self.cursors.get(dataset)
        return datetime.fromisoformat(value) if value else None

    def prune(self, now: datetime) -> None:
        for dataset, keys in self.seen.items():
            cutoff = now - SEEN_RETENTION
            cursor = self.cursor(dataset)
            if cursor is not None:
                # A backfill still walking through old years re-reads a short overlap behind its cursor.
                cutoff = min(cutoff, cursor - timedelta(days=7))
            self.seen[dataset] = {k: t for k, t in keys.items() if datetime.fromisoformat(t) >= cutoff}

    def to_json(self) -> str:
        return json.dumps({"seen": self.seen, "cursors": self.cursors}, indent=0, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> State:
        raw = json.loads(text)
        return cls(seen=raw.get("seen", {}), cursors=raw.get("cursors", {}))


class LocalBackend:
    def __init__(self, root: Path):
        self.root = root

    def write(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def read(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None


class S3Backend:
    def __init__(self, bucket: str, prefix: str):
        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def write(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def read(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None


class Store:
    def __init__(self, uri: str):
        self.uri = uri
        parsed = urlparse(uri)
        if parsed.scheme == "s3":
            self.backend = S3Backend(parsed.netloc, parsed.path)
        elif parsed.scheme in ("", "file"):
            self.backend = LocalBackend(Path(parsed.path if parsed.scheme else uri))
        else:
            raise ValueError(f"unsupported archive URI: {uri}")

    def load_state(self) -> State:
        data = self.backend.read(STATE_KEY)
        return State.from_json(data.decode()) if data else State()

    def save_state(self, state: State) -> None:
        self.backend.write(STATE_KEY, state.to_json().encode())

    def write_normalized(self, dataset: str, month: str, run_id: str, frame) -> str:
        key = f"normalized/{dataset}/month={month}/{run_id}.parquet"
        buf = io.BytesIO()
        pq.write_table(to_table(frame), buf, compression=PARQUET_COMPRESSION)
        self.backend.write(key, buf.getvalue())
        return key

    def write_raw(self, dataset: str, month: str, run_id: str, records: list[dict]) -> str:
        key = f"raw/{dataset}/month={month}/{run_id}.jsonl.zst"
        lines = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()
        self.backend.write(key, zstandard.ZstdCompressor(level=10).compress(lines))
        return key


def read_raw(data: bytes) -> list[dict]:
    """Decode a raw/*.jsonl.zst file (a standard zstd frame; `zstd -d` also works)."""
    # stream_reader also handles frames without a content size, as written by earlier versions.
    text = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read().decode()
    return [json.loads(line) for line in text.splitlines() if line]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
