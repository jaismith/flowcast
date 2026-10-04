"""Site registry (pipeline/sites.yaml, serving fields) and model registry (lake `models/`).

Model registry layout (production-architecture.md §3.2), all under the lake:

    models/{family}/{version}/manifest.json   files with SHA-256, source runs, git SHA, notes
    models/{family}/{version}/...             the files the manifest lists
    models/production.json                    {"flow": v, "temp": v, "snow": v}: what forecast runs use
    models/history/{UTC stamp}.json           every pointer ever written, for rollback

Versions are immutable. Promotion (`flowcast-serve promote`) writes a new version and, with `--activate`, the
pointer; rollback writes an older pointer. Forecast runs read the pointer at the start of every run, so a swap
takes effect at the next run.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml

FAMILIES = ("flow", "temp", "snow")
POINTER = "models/production.json"
DEFAULT_SITES = Path(os.environ.get("FLOWCAST_SITES") or Path(__file__).resolve().parents[3] / "pipeline" / "sites.yaml")


@dataclass(frozen=True)
class ServedSite:
    usgs_id: str  # digits only, the cube's basin id
    slug: str | None
    name: str
    short_name: str
    lat: float
    lon: float
    timezone: str
    pinned: bool
    has_temperature: bool
    nws_lid: str | None

    @property
    def site_id(self) -> str:
        return f"USGS-{self.usgs_id}"


INDEX_KEY = "sites/index.json"
_INDEX_TTL_S = 300.0
_index_cache: dict[str, tuple[float, dict[str, ServedSite]]] = {}


def registry_overrides(path: Path | str = DEFAULT_SITES) -> dict[str, ServedSite]:
    """Sites with serving fields in pipeline/sites.yaml (slug, display name, pinned), keyed by site id."""
    raw = yaml.safe_load(Path(path).read_text())
    out = {}
    for e in raw.get("sites", []):
        if not e.get("slug"):
            continue
        site = ServedSite(
            usgs_id=str(e["id"]).removeprefix("USGS-"),
            slug=e["slug"],
            name=e["name"],
            short_name=e.get("short_name", e["name"]),
            lat=float(e["lat"]),
            lon=float(e["lon"]),
            timezone=e.get("timezone", "America/New_York"),
            pinned=bool(e.get("pinned", False)),
            has_temperature="water_temperature" in e.get("variables", []),
            nws_lid=e.get("nws_lid"),
        )
        out[site.site_id] = site
    return out


def sites_from_index(entries: list[dict], path: Path | str = DEFAULT_SITES) -> dict[str, ServedSite]:
    """Every site of the index (`site_index.build`), with pipeline/sites.yaml's serving fields where it has them."""
    overrides = registry_overrides(path)
    out = {}
    for e in entries:
        o = overrides.get(e["id"])
        out[e["id"]] = o or ServedSite(
            usgs_id=e["id"].removeprefix("USGS-"), slug=e.get("slug"), name=e["name"], short_name=e.get("town") or e["name"],
            lat=float(e["lat"]), lon=float(e["lon"]), timezone="America/New_York", pinned=False,
            has_temperature=bool(e.get("has_temp")), nws_lid=None,
        )
    return out


def served_sites(lake_bucket: str | None = None, path: Path | str = DEFAULT_SITES) -> dict[str, ServedSite]:
    """Every servable site keyed by id: the lake's site index (cached 5 min per container), else the YAML sites."""
    bucket = lake_bucket if lake_bucket is not None else os.environ.get("LAKE_URI", "").removeprefix("s3://").split("/", 1)[0]
    if not bucket:
        return registry_overrides(path)
    hit = _index_cache.get(bucket)
    if hit and time.monotonic() - hit[0] < _INDEX_TTL_S:
        return hit[1]
    s3 = boto3.client("s3")
    try:
        entries = json.loads(s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read())["sites"]
    except s3.exceptions.NoSuchKey:
        entries = []
    sites = sites_from_index(entries, path) if entries else registry_overrides(path)
    _index_cache[bucket] = (time.monotonic(), sites)
    return sites


def resolve(site: str, sites: dict[str, ServedSite] | None = None) -> ServedSite | None:
    """A servable site by id (`USGS-01427510`, `01427510`) or slug (`callicoon`)."""
    site = site.strip()
    sites = sites if sites is not None else served_sites()
    key = site.upper() if site.upper().startswith("USGS-") else f"USGS-{site}" if site.isdigit() else None
    if key:
        return sites.get(key)
    return next((s for s in sites.values() if s.slug == site.lower()), None)


# ---------------------------------------------------------------------------------------------- model registry


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class ModelRegistry:
    """Reads and writes `models/` in the lake bucket; downloads versions into a local cache, verifying hashes."""

    def __init__(self, bucket: str, prefix: str = "", cache: Path | str = "/tmp/flowcast/models"):
        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.cache = Path(cache)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_json(self, key: str) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None

    def write_json(self, key: str, doc: dict) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=json.dumps(doc, indent=1).encode(), ContentType="application/json")

    def production(self) -> dict[str, str]:
        doc = self.read_json(POINTER)
        if not doc:
            raise RuntimeError(f"no model pointer at s3://{self.bucket}/{self._key(POINTER)}; promote a version first")
        return {f: doc[f] for f in FAMILIES if doc.get(f)}

    def set_production(self, pointer: dict[str, str], note: str = "") -> dict:
        for family, version in pointer.items():
            if self.read_json(f"models/{family}/{version}/manifest.json") is None:
                raise KeyError(f"models/{family}/{version} does not exist")
        doc = {**pointer, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "note": note}
        self.write_json(f"models/history/{doc['updated'].replace(':', '')}.json", doc)
        self.write_json(POINTER, doc)
        return doc

    def history(self) -> list[dict]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key("models/history/")):
            for obj in page.get("Contents", []):
                out.append(json.loads(self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()))
        return sorted(out, key=lambda d: d["updated"])

    def fetch(self, family: str, version: str) -> tuple[Path, dict]:
        """Local copy of a version (cached across warm invocations); every file's SHA-256 is checked once."""
        root = self.cache / family / version
        manifest = self.read_json(f"models/{family}/{version}/manifest.json") if not (root / "manifest.json").exists() else json.loads((root / "manifest.json").read_text())
        if manifest is None:
            raise KeyError(f"models/{family}/{version} has no manifest")
        if not (root / ".verified").exists():
            for rel, digest in manifest["files"].items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, self._key(f"models/{family}/{version}/{rel}"), str(path))
                if sha256_file(path) != digest:
                    raise RuntimeError(f"models/{family}/{version}/{rel}: SHA-256 mismatch")
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / ".verified").write_text("ok")
        return root, manifest
