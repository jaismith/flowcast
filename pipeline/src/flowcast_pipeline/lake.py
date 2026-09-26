"""Object storage for the flowcast data lake, addressed by a URI so the backend is configuration.

`s3://bucket/prefix` in AWS, or a local directory (`file:///path` or a plain path) for development
and tests. Keys are `/`-separated paths under the root, e.g. `obs/USGS-01427510/discharge/2026-09.parquet`.
"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urlparse

import boto3
import pandas as pd


class LocalBackend:
    def __init__(self, root: Path):
        self.root = root

    def read(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def write(self, key: str, data: bytes, content_type: str | None = None, cache_control: str | None = None) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if base.is_file():
            return [prefix]
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))


class S3Backend:
    def __init__(self, bucket: str, prefix: str):
        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def write(self, key: str, data: bytes, content_type: str | None = None, cache_control: str | None = None) -> None:
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, **extra)

    def list(self, prefix: str) -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            keys.extend(obj["Key"][strip:] for obj in page.get("Contents", []))
        return sorted(keys)


class Lake:
    def __init__(self, uri: str | Path):
        self.uri = str(uri)
        parsed = urlparse(self.uri)
        if parsed.scheme == "s3":
            self.backend: LocalBackend | S3Backend = S3Backend(parsed.netloc, parsed.path)
        elif parsed.scheme in ("", "file"):
            self.backend = LocalBackend(Path(parsed.path if parsed.scheme else self.uri))
        else:
            raise ValueError(f"unsupported lake URI: {uri}")

    def read(self, key: str) -> bytes | None:
        return self.backend.read(key)

    def write(self, key: str, data: bytes, content_type: str | None = None, cache_control: str | None = None) -> None:
        self.backend.write(key, data, content_type, cache_control)

    def list(self, prefix: str) -> list[str]:
        return self.backend.list(prefix)

    def read_parquet(self, key: str) -> pd.DataFrame | None:
        data = self.read(key)
        return pd.read_parquet(io.BytesIO(data)) if data is not None else None

    def write_parquet(self, key: str, frame: pd.DataFrame) -> None:
        buf = io.BytesIO()
        # The AWS SDK for pandas Lambda layer's pyarrow is built without zstd.
        frame.to_parquet(buf, index=False, compression="snappy")
        self.write(key, buf.getvalue(), "application/vnd.apache.parquet")
