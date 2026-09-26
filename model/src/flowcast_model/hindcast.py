"""Issue-time hindcasts in the evaluation interchange format (evaluation/README.md).

For each basin and each issue time on the protocol's cycle (00/06/12/18 UTC by default), the model sees the
hindcast window ending at the issue hour (lagged observed flow is shifted by 1 h, matching the harness's 1 h
observation latency) and forecasts the next `forecast_seq_length` hours. CMAL/GMM/UMAL heads are sampled and
written as ensemble members; regression heads are deterministic. Only the harness's lead grid is written.

Output: hive-partitioned Parquet, `<out>/site_id=<id>/part.parquet`, so scoring can go one site at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.datautils.utils import load_basin_file, load_scaler
from neuralhydrology.modelzoo import get_model
from neuralhydrology.utils.samplingutils import sample_cmal, sample_gmm, sample_umal
from ruamel.yaml import YAML
from torch.utils.data import DataLoader, Subset

from flowcast_eval.protocol import HOURLY_LEADS_H

from .config import load_run
from .cube import Cube, CubeDims
from .dataset import ZarrCubeDataset
from .trainer import best_epoch, latest_checkpoint
from .units import to_cfs

log = logging.getLogger(__name__)
SAMPLERS = {"cmal": sample_cmal, "gmm": sample_gmm, "umal": sample_umal}


def site_id(basin: str) -> str:
    return f"USGS-{basin}" if basin.isdigit() else basin


def choose_epoch(run_dir: Path, spec: str | int | None) -> int:
    if spec in (None, "best"):
        return best_epoch(run_dir) or latest_checkpoint(run_dir)
    if spec == "last":
        return latest_checkpoint(run_dir)
    return int(spec)


def hindcast(run_dir: str | Path, out: str | Path, period: str = "validation", epoch: str | int | None = None, basins: list[str] | None = None, device: str | None = None, n_samples: int | None = None) -> Path:
    run_dir, out = Path(run_dir), Path(out)
    cfg, options = load_run(run_dir)
    ZarrCubeDataset.configure(options.dataset)
    hopts = options.hindcast
    n_samples = n_samples or hopts.n_samples
    epoch = choose_epoch(run_dir, epoch if epoch is not None else hopts.epoch)
    dev = torch.device(device or ("cuda:0" if torch.cuda.is_available() else "cpu"))
    model = get_model(cfg).to(dev)
    model.load_state_dict(torch.load(run_dir / f"model_epoch{epoch:03d}.pt", map_location=dev))
    model.eval()
    scaler = load_scaler(run_dir)
    id_to_int = {}
    if cfg.use_basin_id_encoding:
        with (run_dir / "train_data" / "id_to_int.yml").open() as fp:
            id_to_int = dict(YAML(typ="safe").load(fp))
    target = cfg.target_variables[0]
    center = float(scaler["xarray_feature_center"][target].values)
    scale = float(scaler["xarray_feature_scale"][target].values)
    L = cfg.forecast_seq_length or cfg.predict_last_n
    leads = np.array([h for h in HOURLY_LEADS_H if h <= L], dtype=int)
    head = cfg.head.lower()
    basins = basins or load_basin_file(getattr(cfg, f"{period}_basin_file"))
    cube = Cube(options.dataset.cube, CubeDims.from_dict(options.dataset.dims))
    area_attr = options.target.get("area_attribute")
    areas = cube.load_static(basins, [area_attr])[area_attr] if area_attr else None
    issue_hours = set(hopts.issue_hours)
    model_name = options.model_name
    out.mkdir(parents=True, exist_ok=True)
    for basin in basins:
        try:
            ds = get_dataset(cfg, is_train=False, period=period, basin=basin, scaler=scaler, id_to_int=id_to_int)
        except Exception as err:  # NoEvaluationDataError and friends: skip the basin
            log.warning("skipping %s: %s", basin, err)
            continue
        positions = [i for i, (_, _, t) in enumerate(ds.sample_dates()) if t.hour in issue_hours and t.minute == 0]
        if not positions:
            continue
        loader = DataLoader(Subset(ds, positions), batch_size=hopts.batch_size, collate_fn=ds.collate_fn)
        issues, values = [], []
        with torch.no_grad():
            for data in loader:
                dates = data["date"]
                for key in data:
                    if key.startswith("x_d"):
                        data[key] = {k: v.to(dev) for k, v in data[key].items()}
                    elif not key.startswith("date"):
                        data[key] = data[key].to(dev)
                data = model.pre_model_hook(data, is_train=False)
                if head in SAMPLERS:
                    y = SAMPLERS[head](model, data, n_samples, scaler)["y_hat"][:, -L:, 0, :]
                else:
                    y = model(data)["y_hat"][:, -L:, :1]
                y = y.detach().cpu().numpy()[:, leads - 1, :] * scale + center
                values.append(np.clip(y, 0.0, None))
                issues.append(dates[:, -L - 1])
        values = np.concatenate(values).astype(np.float32)
        issue_times = pd.DatetimeIndex(np.concatenate(issues)).tz_localize("UTC")
        cfs = to_cfs(values, options.target.get("unit", "mm/h"), None if areas is None else float(areas[basin]))
        n, l, m = cfs.shape
        frame = pd.DataFrame(
            {
                "site_id": site_id(basin),
                "variable": "discharge",
                "model": model_name,
                "issue_time": np.repeat(issue_times.values, l * m),
                "lead_h": np.tile(np.repeat(leads.astype(float), m), n),
                "value": cfs.ravel(),
                "unit": "ft3/s",
                "run_type": options.run_type,
            }
        )
        frame["issue_time"] = pd.to_datetime(frame["issue_time"], utc=True)
        frame["valid_time"] = frame["issue_time"] + pd.to_timedelta(frame["lead_h"], unit="h")
        if m > 1:
            frame["member"] = np.tile(np.arange(m), n * l)
        part = out / f"site_id={site_id(basin)}"
        part.mkdir(parents=True, exist_ok=True)
        frame.drop(columns=["site_id"]).to_parquet(part / f"{model_name}.parquet", index=False)
        log.info("%s: %d issues x %d leads x %d members (epoch %d)", basin, n, l, m, epoch)
    (out / "_hindcast.json").write_text(pd.Series({"run_dir": str(run_dir), "epoch": epoch, "period": period, "model": model_name, "n_samples": n_samples}).to_json())
    return out
