"""Issue-time hindcasts in the evaluation interchange format (evaluation/README.md).

For each basin and each issue time on the protocol's cycle (00/06/12/18 UTC by default), the model sees the
hindcast window ending at the issue hour (lagged observed flow is shifted by 1 h, matching the harness's 1 h
observation latency) and forecasts the next `forecast_seq_length` hours. CMAL/GMM/UMAL heads are sampled and
written as ensemble members; regression heads are deterministic. Only the harness's lead grid is written.

Output: hive-partitioned Parquet, `<out>/site_id=<id>/part.parquet`, so scoring can go one site at a time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
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

from .config import HindcastMode, load_run
from .cube import Cube, CubeDims
from .dataset import ZarrCubeDataset
from .models import apply_variants
from .trainer import best_epoch, latest_checkpoint
from .units import to_harness_unit

log = logging.getLogger(__name__)
SAMPLERS = {"cmal": sample_cmal, "gmm": sample_gmm, "umal": sample_umal}


def sample_mixture(pred: dict, head: str, positions: torch.Tensor, n_distributions: int, n_samples: int) -> torch.Tensor:
    """Samples [batch, len(positions), n_samples] (normalized) of the first target at the given sequence positions.

    Same draws as NeuralHydrology's samplers (component by weight, then the component's inverse CDF), vectorized
    over positions instead of looping over every step of the output sequence.
    """
    k = n_distributions
    pi = pred["pi"][:, positions, :k]
    B, P, _ = pi.shape
    comp = torch.multinomial(pi.reshape(-1, k), n_samples, replacement=True)

    def pick(name: str) -> torch.Tensor:
        return pred[name][:, positions, :k].reshape(-1, k).gather(1, comp)

    if head == "cmal":
        m, b, t = pick("mu"), pick("b"), pick("tau")
        u = torch.rand_like(m).clamp(1e-6, 1 - 1e-6)
        x = torch.where(u < t, m + b * torch.log(u / t) / (1 - t), m - b * torch.log((1 - u) / (1 - t)) / t)
    elif head == "gmm":
        x = pick("mu") + pick("sigma") * torch.randn(B * P, n_samples, device=pi.device)
    else:
        raise NotImplementedError(head)
    return x.reshape(B, P, n_samples)


def site_id(basin: str) -> str:
    return f"USGS-{basin}" if basin.isdigit() else basin


def choose_epoch(run_dir: Path, spec: str | int | None) -> int:
    if spec in (None, "best"):
        return best_epoch(run_dir) or latest_checkpoint(run_dir)
    if spec == "last":
        return latest_checkpoint(run_dir)
    return int(spec)


def _load_extra_issues(path: str | Path | None) -> dict[str, list[pd.Timestamp]]:
    """Extra issue times per site (e.g. MARFC bulletin times) from Parquet with `site_id` and `issue_time`."""
    if not path:
        return {}
    df = pd.read_parquet(path)
    df["issue_time"] = pd.to_datetime(df["issue_time"], utc=True)
    return {str(s).removeprefix("USGS-"): sorted(g["issue_time"].unique()) for s, g in df.groupby("site_id")}


def hindcast(
    run_dir: str | Path,
    out: str | Path,
    period: str = "validation",
    epoch: str | int | None = None,
    basins: list[str] | None = None,
    device: str | None = None,
    n_samples: int | None = None,
    extra_issues: str | Path | None = None,
    cube_paths: list[str] | None = None,
) -> Path:
    run_dir, out = Path(run_dir), Path(out)
    cfg, options = load_run(run_dir)
    if cube_paths:
        options.dataset.cube = [str(p) for p in cube_paths]
    hopts = options.hindcast
    n_samples = n_samples or hopts.n_samples
    epoch = choose_epoch(run_dir, epoch if epoch is not None else hopts.epoch)
    dev = torch.device(device or ("cuda:0" if torch.cuda.is_available() else "cpu"))
    model = apply_variants(get_model(cfg), options.model).to(dev)
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
    variable = options.target.get("variable", "discharge")
    clip_min = options.target.get("clip_min", 0.0)
    daily = options.target.get("daily_max")
    # daily maxima need every hour of the forecast window; hourly output keeps the harness's lead grid
    out_leads = np.arange(1, L + 1) if daily else leads
    grid = np.searchsorted(out_leads, leads)
    head = cfg.head.lower()
    basins = basins or load_basin_file(getattr(cfg, f"{period}_basin_file"))
    cube = Cube(options.dataset.cube, CubeDims.from_dict(options.dataset.dims))
    area_attr = options.target.get("area_attribute")
    areas = cube.load_static(basins, [area_attr])[area_attr] if area_attr else None
    issue_hours = set(hopts.issue_hours)
    start = pd.Timestamp(hopts.start) if hopts.start else None
    extra = _load_extra_issues(extra_issues or hopts.extra_issues)
    modes = hopts.modes or {"default": HindcastMode(run_type=options.run_type)}
    out.mkdir(parents=True, exist_ok=True)

    def predict(ds, positions: list[int]) -> tuple[np.ndarray, pd.DatetimeIndex]:
        workers = min(4, max(0, (os.cpu_count() or 1) - 1))
        loader = DataLoader(Subset(ds, positions), batch_size=hopts.batch_size, collate_fn=ds.collate_fn, num_workers=workers)
        lead_pos = torch.as_tensor(cfg.seq_length - L + out_leads - 1, device=dev)
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
                pred = model(data)
                if head in ("cmal", "gmm"):
                    y = sample_mixture(pred, head, lead_pos, cfg.n_distributions, n_samples)
                elif head in SAMPLERS:
                    y = SAMPLERS[head](model, data, n_samples, scaler)["y_hat"][:, -L:, 0, :][:, out_leads - 1, :]
                else:
                    y = pred["y_hat"][:, lead_pos, :1]
                y = y.detach().cpu().numpy() * scale + center
                values.append(y if clip_min is None else np.clip(y, clip_min, None))
                issues.append(dates[:, -L - 1])
        return np.concatenate(values).astype(np.float32), pd.DatetimeIndex(np.concatenate(issues))

    for mode_name, mode in modes.items():
        ZarrCubeDataset.configure(
            replace(
                options.dataset,
                mask_hindcast=list(mode.mask_hindcast),
                mask_forecast=list(mode.mask_forecast),
                substitute_forecast=dict(mode.substitute_forecast),
                forecast_latency_h={**options.dataset.forecast_latency_h, **mode.forecast_latency_h},
            )
        )
        model_name = f"{options.model_name}{mode.suffix}"
        for basin in basins:
            target = out / f"site_id={site_id(basin)}" / f"{model_name}.parquet"
            if hopts.resume and target.exists():
                continue
            try:
                ds = get_dataset(cfg, is_train=False, period=period, basin=basin, scaler=scaler, id_to_int=id_to_int)
            except Exception as err:  # NoEvaluationDataError and friends: skip the basin
                log.warning("skipping %s: %s", basin, err)
                continue
            labels_by_hour: dict[pd.Timestamp, list[pd.Timestamp]] = {}
            for t in extra.get(basin, []):
                labels_by_hour.setdefault(pd.Timestamp(t).tz_convert(None).floor("h"), []).append(pd.Timestamp(t))
            positions, labels = [], []
            sample_dates = ds.sample_dates()
            for i, (_, _, t) in enumerate(sample_dates):
                if start is not None and t < start:
                    continue
                own = [pd.Timestamp(t, tz="UTC")] if t.hour in issue_hours and t.minute == 0 else []
                extra_labels = [x for x in labels_by_hour.get(t, []) if x not in own]
                if own or extra_labels:
                    positions.append(i)
                    labels.append(own + extra_labels)
            if not positions:
                continue
            runs = []
            for member in mode.members or [None]:
                ZarrCubeDataset.options.forecast_member = member
                runs.append(predict(ds, positions)[0])
            values = np.concatenate(runs, axis=2)
            hours = pd.DatetimeIndex([sample_dates[p][2] for p in positions]).tz_localize("UTC")
            values, unit = to_harness_unit(values, options.target.get("unit", "mm/h"), None if areas is None else float(areas[basin]))
            row_pos = np.repeat(np.arange(len(positions)), [len(x) for x in labels])
            label_times = pd.DatetimeIndex([x for xs in labels for x in xs])
            values, base = values[row_pos], hours[row_pos]
            frame = _long_frame(values[:, grid], base, label_times, leads, variable, model_name, unit, mode.run_type)
            if daily:
                maxima, days, day_leads = daily_maxima(values, base, daily.get("timezone", "America/New_York"))
                frame = pd.concat([frame, _daily_frame(maxima, days, day_leads, label_times, daily.get("variable", f"{variable}_daily_max"), model_name, unit, mode.run_type)], ignore_index=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".parquet.tmp")
            frame.to_parquet(tmp, index=False)
            tmp.replace(target)
            n, l, m = values[:, grid].shape
            log.info("%s %s: %d issues x %d leads x %d members (epoch %d)", mode_name, basin, n, l, m, epoch)
    ZarrCubeDataset.configure(options.dataset)
    (out / "_hindcast.json").write_text(pd.Series({"run_dir": str(run_dir), "epoch": epoch, "period": period, "model": options.model_name, "modes": list(modes), "n_samples": n_samples}).to_json())
    return out


def _long_frame(values: np.ndarray, base: pd.DatetimeIndex, labels: pd.DatetimeIndex, leads: np.ndarray, variable: str, model: str, unit: str, run_type: str) -> pd.DataFrame:
    n, l, m = values.shape
    valid = np.repeat(base.values, l * m) + np.tile(np.repeat(leads.astype("timedelta64[h]"), m), n)
    frame = pd.DataFrame(
        {
            "variable": variable,
            "model": model,
            "issue_time": np.repeat(labels.values, l * m),
            "valid_time": valid,
            "value": values.ravel(),
            "unit": unit,
            "run_type": run_type,
        }
    )
    frame["issue_time"] = pd.to_datetime(frame["issue_time"], utc=True)
    frame["valid_time"] = pd.to_datetime(frame["valid_time"], utc=True)
    frame["lead_h"] = (frame["valid_time"] - frame["issue_time"]).dt.total_seconds() / 3600.0
    if m > 1:
        frame["member"] = np.tile(np.arange(m), n * l)
    return frame


# A day's maximum is only forecast when the window covers the local hours it usually falls in.
DAYTIME_HOURS = range(10, 20)


def local_days(hour_ending: pd.DatetimeIndex, timezone: str) -> tuple[np.ndarray, np.ndarray]:
    """Local calendar date (as datetime64[D]) and local hour of hour-ending UTC values (each covers the hour before)."""
    start = pd.DatetimeIndex(hour_ending).tz_localize("UTC") if pd.DatetimeIndex(hour_ending).tz is None else pd.DatetimeIndex(hour_ending)
    local = (start - pd.Timedelta(hours=1)).tz_convert(timezone)
    return local.tz_localize(None).normalize().values.astype("datetime64[D]"), local.hour.to_numpy()


def daily_maxima(values: np.ndarray, base: pd.DatetimeIndex, timezone: str = "America/New_York") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-member daily maxima of hourly forecasts `values[issue, hour 1..L, member]` issued at `base` (UTC, naive).

    Returns (maxima[issue, day, member], local dates[issue, day], lead days [day]); day k is the issue's local date
    plus k. NaN where the window misses part of that day's daytime hours.
    """
    n, L, m = values.shape
    hours = base.values[:, None] + np.arange(1, L + 1).astype("timedelta64[h]")[None, :]
    dates, local_hour = local_days(pd.DatetimeIndex(hours.ravel()), timezone)
    dates, local_hour = dates.reshape(n, L), local_hour.reshape(n, L)
    issue_date, _ = local_days(pd.DatetimeIndex(base) + pd.Timedelta(hours=1), timezone)
    day = (dates - issue_date[:, None]).astype(int)
    n_days = int(day.max()) + 1
    maxima = np.full((n, n_days, m), np.nan, dtype=np.float32)
    daytime = np.isin(local_hour, DAYTIME_HOURS)
    for k in range(n_days):
        on = day == k
        covered = (on & daytime).sum(axis=1) == len(DAYTIME_HOURS)
        if not covered.any():
            continue
        v = np.where(on[..., None], values, -np.inf).max(axis=1)
        maxima[covered, k] = v[covered]
    days = issue_date[:, None] + np.arange(n_days).astype("timedelta64[D]")[None, :]
    return maxima, days, np.arange(n_days)


def _daily_frame(maxima: np.ndarray, days: np.ndarray, day_leads: np.ndarray, labels: pd.DatetimeIndex, variable: str, model: str, unit: str, run_type: str) -> pd.DataFrame:
    """Daily rows in the interchange convention: valid_time is midnight UTC of the local date, lead_h = 24 x day."""
    n, d, m = maxima.shape
    frame = pd.DataFrame(
        {
            "variable": variable,
            "model": model,
            "issue_time": pd.to_datetime(np.repeat(labels.values, d * m), utc=True),
            "valid_time": pd.to_datetime(np.repeat(days.ravel().astype("datetime64[ns]"), m), utc=True),
            "value": maxima.ravel(),
            "unit": unit,
            "run_type": run_type,
            "lead_h": np.tile(np.repeat(24.0 * day_leads, m), n),
        }
    )
    if m > 1:
        frame["member"] = np.tile(np.arange(m), n * d)
    return frame.dropna(subset=["value"]).reset_index(drop=True)
