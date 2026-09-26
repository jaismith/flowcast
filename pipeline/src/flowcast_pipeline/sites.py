"""Site registry (pipeline/sites.yaml)."""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .usgs.params import site_id

# Lambda bundles place the registry beside the packages and point FLOWCAST_SITES at it.
DEFAULT_REGISTRY = Path(os.environ.get("FLOWCAST_SITES") or Path(__file__).resolve().parents[2] / "sites.yaml")


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    timezone: str = "UTC"
    variables: tuple[str, ...] = ("discharge",)
    nws_lid: str | None = None
    nwm_reach: int | None = None
    stage_thresholds_ft: dict[str, float] = field(default_factory=dict)
    regulation_gauges: tuple[str, ...] = ()
    upstream_gauges: tuple[str, ...] = ()


def load_sites(path: Path | str = DEFAULT_REGISTRY) -> dict[str, Site]:
    raw = yaml.safe_load(Path(path).read_text())
    sites = {}
    for entry in raw.get("sites", []):
        site = Site(
            id=site_id(entry["id"]),
            name=entry["name"],
            timezone=entry.get("timezone", "UTC"),
            variables=tuple(entry.get("variables", ["discharge"])),
            nws_lid=entry.get("nws_lid"),
            nwm_reach=entry.get("nwm_reach"),
            stage_thresholds_ft=dict(entry.get("stage_thresholds_ft", {})),
            regulation_gauges=tuple(site_id(g) for g in entry.get("regulation_gauges", [])),
            upstream_gauges=tuple(site_id(g) for g in entry.get("upstream_gauges", [])),
        )
        sites[site.id] = site
    return sites


def get_site(site: str, path: Path | str = DEFAULT_REGISTRY) -> Site:
    return load_sites(path)[site_id(site)]


def ingest_gauges(path: Path | str = DEFAULT_REGISTRY) -> list[str]:
    """Every gauge the hourly ingest pulls: sites, their regulation gauges, and the extra `gauges` list."""
    raw = yaml.safe_load(Path(path).read_text())
    ids: list[str] = []
    for site in load_sites(path).values():
        ids += [site.id, *site.regulation_gauges, *site.upstream_gauges]
    ids += [site_id(g["id"]) for g in raw.get("gauges", [])]
    return list(dict.fromkeys(ids))


def registry_document(path: Path | str = DEFAULT_REGISTRY) -> dict:
    """The registry as JSON-ready data (published as `/v1/sites.json`)."""
    return {"sites": [asdict(site) for site in load_sites(path).values()], "ingest_gauges": ingest_gauges(path)}
