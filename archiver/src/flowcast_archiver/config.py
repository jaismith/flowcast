from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_POINTS_FILE = Path(__file__).with_name("forecast_points.yaml")


@dataclass(frozen=True)
class Point:
    lid: str
    name: str
    kind: str
    usgs: str | None = None
    reach: str | None = None
    hefs: bool = False


@dataclass(frozen=True)
class Config:
    points: tuple[Point, ...]
    rvf_pils: tuple[str, ...]
    tva_sites: tuple[str, ...] = ()

    @property
    def by_lid(self) -> dict[str, Point]:
        return {p.lid: p for p in self.points}

    @property
    def reaches(self) -> dict[str, Point]:
        return {p.reach: p for p in self.points if p.reach}


def load_config(path: Path | str = DEFAULT_POINTS_FILE) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    points = tuple(Point(**p) for p in raw["points"])
    tva_sites = tuple(s["lid"] for s in raw.get("tva_sites", []))
    return Config(points=points, rvf_pils=tuple(raw["rvf_pils"]), tva_sites=tva_sites)
