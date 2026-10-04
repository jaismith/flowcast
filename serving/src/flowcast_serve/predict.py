"""Model inference on a live cube with the hindcast path's own pieces (`flowcast_model.hindcast`), one issue.

Per seed run directory (a registry version holds `seeds/<run>/`): the saved NeuralHydrology config, scaler and final
checkpoint; the `operational` hindcast mode (AORC masked, GEFS substituted into the forecast branch, the 6 h GEFS
latency, the forward fill of lagged flow); every GEFS member; `n_samples` draws per member, sampled as the hindcast
does (`sample_mixture`, coherent paths where the config asks for them); the CMAL mixture is kept too.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from flowcast_model.config import load_run
from flowcast_model.dataset import ZarrCubeDataset
from flowcast_model.hindcast import choose_epoch, mixture_params, sample_mixture
from flowcast_model.models import apply_variants
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.datautils.utils import load_scaler
from neuralhydrology.modelzoo import get_model

log = logging.getLogger(__name__)
torch.set_num_threads(2)


@dataclass
class SeedOutput:
    run: str
    samples: np.ndarray  # [lead, member * n_samples] in the target unit (mm/h or degC), clipped as the hindcast clips
    mixture: np.ndarray  # [lead, member, k, 4] (pi, mu, b, tau), mu and b in the target unit
    members: list[int]
    n_samples: int


@lru_cache(maxsize=8)
def _load(seed_dir: str):
    run_dir = Path(seed_dir)
    cfg, options = load_run(run_dir)
    epoch = final_epoch(run_dir, options.hindcast.epoch)
    model = apply_variants(get_model(cfg), options.model)
    model.load_state_dict(torch.load(run_dir / f"model_epoch{epoch:03d}.pt", map_location="cpu"))
    model.eval()
    return cfg, options, model, load_scaler(run_dir), epoch


def final_epoch(run_dir: Path, spec: str | int | None) -> int:
    """`hindcast.choose_epoch`, except that "last" is the newest saved checkpoint: registry versions keep only the
    final weights (no optimizer states, which `trainer.latest_checkpoint` requires)."""
    if spec == "last" or spec is None:
        epochs = [int(p.stem.removeprefix("model_epoch")) for p in Path(run_dir).glob("model_epoch*.pt")]
        if not epochs:
            raise FileNotFoundError(f"no checkpoint in {run_dir}")
        return max(epochs)
    return choose_epoch(Path(run_dir), spec)


def _nh_date(t: pd.Timestamp) -> str:
    return t.strftime("%d/%m/%Y")


def predict_seed(seed_dir: Path, cube_path: Path, basin: str, issue: pd.Timestamp, n_samples: int | None = None, seed: int = 0,
                 mode_name: str = "operational") -> SeedOutput:
    """Forecast of one basin at one issue (UTC) from one seed's run directory, on the live cube at `cube_path`."""
    cfg0, options, model, scaler, epoch = _load(str(seed_dir))
    cfg = copy.deepcopy(cfg0)
    issue_naive = issue.tz_convert(None) if issue.tzinfo else issue
    L = cfg.forecast_seq_length or cfg.predict_last_n
    cfg.update_config({
        "test_start_date": _nh_date(issue_naive.floor("D")),
        "test_end_date": _nh_date((issue_naive + pd.Timedelta(hours=L)).floor("D")),
        "data_dir": str(cube_path),
        "num_workers": 0,
    }, dev_mode=True)
    hopts = options.hindcast
    mode = hopts.modes[mode_name]
    ZarrCubeDataset.configure(replace(
        options.dataset,
        cube=[str(cube_path)],
        allow_frozen_test=True,  # the live cube holds only live data (issues >= 2026-10-01)
        mask_hindcast=list(mode.mask_hindcast),
        mask_forecast=list(mode.mask_forecast),
        substitute_forecast=dict(mode.substitute_forecast),
        forecast_latency_h={**options.dataset.forecast_latency_h, **mode.forecast_latency_h},
        ffill_hindcast_h={**options.dataset.ffill_hindcast_h, **mode.ffill_hindcast_h},
        read_threads=1,
        cache_basins=1,
    ))
    try:
        ds = get_dataset(cfg, is_train=False, period="test", basin=basin, scaler=scaler, id_to_int={})
        positions = [i for i, (_, _, t) in enumerate(ds.sample_dates()) if t == issue_naive]
        if not positions:
            raise RuntimeError(f"no sample at issue {issue_naive} for {basin} in {cube_path}")
        target = cfg.target_variables[0]
        center = float(scaler["xarray_feature_center"][target].values)
        scale = float(scaler["xarray_feature_scale"][target].values)
        clip_min = options.target.get("clip_min", 0.0)
        n = n_samples or hopts.n_samples
        lead_pos = torch.as_tensor(cfg.seq_length - L + np.arange(1, L + 1) - 1)
        members = list(mode.members or [0])
        torch.manual_seed(seed)
        draws, mixes = [], []
        with torch.no_grad():
            for member in members:
                ZarrCubeDataset.options.forecast_member = member
                data = ds.collate_fn([ds[positions[0]]])
                for key in list(data):
                    if key.startswith("x_d"):
                        data[key] = {k: v for k, v in data[key].items()}
                data = model.pre_model_hook(data, is_train=False)
                pred = model(data)
                y = sample_mixture(pred, cfg.head.lower(), lead_pos, cfg.n_distributions, n, coherent=hopts.coherent_samples)
                y = y.numpy()[0] * scale + center  # [L, n]
                draws.append(y if clip_min is None else np.clip(y, clip_min, None))
                mixes.append(mixture_params(pred, lead_pos, cfg.n_distributions, center, scale)[0])  # [L, k, 4]
    finally:
        ZarrCubeDataset.configure(options.dataset)
    log.info("%s %s @ %s: %d members x %d draws (epoch %d)", Path(seed_dir).name, basin, issue_naive, len(members), n, epoch)
    return SeedOutput(Path(seed_dir).name, np.concatenate(draws, axis=1).astype(np.float32), np.stack(mixes, axis=1), members, n)


def seed_dirs(version_root: Path) -> list[Path]:
    return sorted(p for p in (version_root / "seeds").iterdir() if p.is_dir())


def required_features(version_root: Path) -> dict[str, list[str]]:
    """What a version's seeds read: dynamic inputs, targets, lagged/persisted sources, statics, forecast substitutes."""
    out: dict[str, set] = {"dynamic": set(), "static": set(), "forecast": set()}
    for d in seed_dirs(version_root):
        cfg, options = load_run(d)
        dyn = set(cfg.dynamic_inputs_flattened if isinstance(cfg.dynamic_inputs, list) else [i for v in cfg.dynamic_inputs.values() for i in v])
        lagged = {f"{f}_shift{s}" for f, sh in cfg.lagged_features.items() for s in (sh if isinstance(sh, list) else [sh])}
        persisted = set(options.dataset.persist_inputs)
        out["dynamic"] |= (dyn - lagged - persisted) | set(cfg.lagged_features) | set(cfg.target_variables)
        out["static"] |= set(cfg.static_attributes)
        for mode in options.hindcast.modes.values():
            out["forecast"] |= set(mode.substitute_forecast.values())
            out["dynamic"] -= set(mode.substitute_forecast.values())
    return {k: sorted(v) for k, v in out.items()}
