"""Training configs: a NeuralHydrology config plus a `flowcast:` block.

```yaml
flowcast:
  dataset:            # DatasetOptions (dataset.py)
    cube: [/data/cube.zarr]
    optional_inputs: [qobs_mm_h_shift1]
    group_dropout: {qobs_mm_h_shift1: 0.5}
  basins: all         # all | [ids] | path to a basin file; same list for train/validation/test
  target: {unit: mm/h, area_attribute: area_km2}   # how to convert the target to ft3/s for scoring
  hindcast: {issue_hours: [0, 6, 12, 18], n_samples: 50, epoch: best}
  run_type: perfect_forcing                        # interchange run_type of hindcasts
experiment_name: ...  # everything else is NeuralHydrology
model: handoff_forecast_lstm
```

Sweep overrides use dotted keys (`flowcast.dataset.group_dropout.qobs_mm_h_shift1: 0.3`, `hidden_size: 128`).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from neuralhydrology.utils.config import Config

from .cube import Cube, CubeDims
from .dataset import DatasetOptions

FLOWCAST_FILE = "flowcast.yml"
NH_CONFIG_FILE = "config.yml"


@dataclass
class HindcastOptions:
    issue_hours: list[int] = field(default_factory=lambda: [0, 6, 12, 18])
    n_samples: int = 50
    epoch: str | int = "best"
    batch_size: int = 256
    enabled: bool = True


@dataclass
class FlowcastOptions:
    dataset: DatasetOptions
    basins: Any = "all"
    target: dict = field(default_factory=lambda: {"unit": "mm/h", "area_attribute": "area_km2"})
    hindcast: HindcastOptions = field(default_factory=HindcastOptions)
    run_type: str = "perfect_forcing"
    model_name: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "FlowcastOptions":
        d = copy.deepcopy(d or {})
        return cls(
            dataset=DatasetOptions.from_dict(d.pop("dataset", {})),
            hindcast=HindcastOptions(**d.pop("hindcast", {})),
            **d,
        )

    def to_dict(self) -> dict:
        return {
            "dataset": vars(self.dataset),
            "basins": self.basins,
            "target": self.target,
            "hindcast": vars(self.hindcast),
            "run_type": self.run_type,
            "model_name": self.model_name,
        }


def set_dotted(d: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def apply_overrides(raw: dict, overrides: dict | None) -> dict:
    raw = copy.deepcopy(raw)
    for k, v in (overrides or {}).items():
        set_dotted(raw, k, v)
    return raw


def read_raw(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _basin_list(spec: Any, cube: Cube) -> list[str]:
    if spec in (None, "all"):
        return cube.basins
    if isinstance(spec, list):
        return [str(b) for b in spec]
    return [line.strip() for line in Path(spec).read_text().splitlines() if line.strip()]


def prepare_run(raw: dict, run_dir: str | Path, cube_paths: list[str] | None = None) -> tuple[Config, FlowcastOptions]:
    """Resolve a raw training config into an NH Config + FlowcastOptions rooted at a fixed `run_dir`."""
    raw = copy.deepcopy(raw)
    options = FlowcastOptions.from_dict(raw.pop("flowcast", {}))
    if cube_paths:
        options.dataset.cube = [str(p) for p in cube_paths]
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cube = Cube(options.dataset.cube, CubeDims.from_dict(options.dataset.dims))
    basins = _basin_list(options.basins, cube)
    basin_file = run_dir / "basins.txt"
    basin_file.write_text("\n".join(basins) + "\n")
    raw.update(
        {
            "run_dir": str(run_dir),
            "data_dir": str(options.dataset.cube[0]),
            "dataset": raw.get("dataset", "flowcast_zarr"),
            "train_basin_file": str(basin_file),
            "validation_basin_file": str(basin_file),
            "test_basin_file": str(basin_file),
        }
    )
    options.model_name = options.model_name or raw.get("experiment_name", run_dir.name)
    (run_dir / FLOWCAST_FILE).write_text(yaml.safe_dump(options.to_dict(), sort_keys=False))
    return Config(raw), options


def load_run(run_dir: str | Path) -> tuple[Config, FlowcastOptions]:
    run_dir = Path(run_dir)
    cfg = Config(run_dir / NH_CONFIG_FILE)
    options = FlowcastOptions.from_dict(yaml.safe_load((run_dir / FLOWCAST_FILE).read_text()))
    return cfg, options
