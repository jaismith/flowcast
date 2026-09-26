"""On-disk cache for USGS responses.

Chunks that are fully in the past and fully approved are stored permanently; everything else
expires after a TTL chosen by the caller, so provisional data picks up USGS revisions.
"""

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd


def default_cache_dir() -> Path:
    env = os.environ.get("FLOWCAST_CACHE_DIR")
    return Path(env) / "usgs" if env else Path.home() / ".cache" / "flowcast" / "usgs"


class ResponseCache:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else default_cache_dir()

    def _paths(self, key: str, suffix: str) -> tuple[Path, Path]:
        data = self.root / f"{key}{suffix}"
        return data, data.with_name(data.name + ".meta.json")

    def _fresh(self, meta_path: Path, ttl: timedelta | None) -> bool:
        if not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text())
        if meta.get("immutable"):
            return True
        return ttl is not None and time.time() - meta["fetched_at"] < ttl.total_seconds()

    def _write_meta(self, meta_path: Path, immutable: bool) -> None:
        meta_path.write_text(json.dumps({"fetched_at": time.time(), "immutable": immutable}))

    def get_frame(self, key: str, ttl: timedelta | None) -> pd.DataFrame | None:
        data, meta = self._paths(key, ".parquet")
        if data.exists() and self._fresh(meta, ttl):
            return pd.read_parquet(data)
        return None

    def put_frame(self, key: str, frame: pd.DataFrame, immutable: bool) -> None:
        data, meta = self._paths(key, ".parquet")
        data.parent.mkdir(parents=True, exist_ok=True)
        tmp = data.with_name(data.name + ".tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(data)
        self._write_meta(meta, immutable)

    def get_text(self, key: str, ttl: timedelta | None) -> str | None:
        data, meta = self._paths(key, "")
        if data.exists() and self._fresh(meta, ttl):
            return data.read_text()
        return None

    def put_text(self, key: str, text: str, immutable: bool = False) -> None:
        data, meta = self._paths(key, "")
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_text(text)
        self._write_meta(meta, immutable)
