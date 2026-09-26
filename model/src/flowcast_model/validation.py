"""Cheap in-training validation for model selection.

NeuralHydrology's tester runs every hourly window of the validation period and, for CMAL heads, samples the
mixture: about 2 minutes per basin per validation on CPU. This validator instead scores one issue per day
(00 UTC by default) and uses the mixture mean in closed form, so a validation pass costs about 1/24th of an
epoch's forward passes. It reports the average loss (the model-selection criterion) and median-over-basins NSE and
KGE of the forecast at a few leads. Full probabilistic scoring happens afterwards in the issue-time hindcast.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import torch
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.datautils.utils import load_basin_file
from neuralhydrology.utils.config import Config
from neuralhydrology.utils.errors import NoEvaluationDataError
from torch.utils.data import DataLoader, Subset

LOGGER = logging.getLogger(__name__)
LEADS_H = (24, 72, 168)


def mixture_mean(pred: dict, head: str, n_distributions: int = 1) -> torch.Tensor:
    """Predictive mean [batch, seq, 1] of the first target (its mixture components are the first K outputs)."""
    k = n_distributions
    if head == "cmal":
        mu, b, tau, pi = (pred[key][..., :k] for key in ("mu", "b", "tau", "pi"))
        return (pi * (mu + b * (1 - 2 * tau) / (tau * (1 - tau)))).sum(-1, keepdim=True)
    if head == "gmm":
        return (pred["pi"][..., :k] * pred["mu"][..., :k]).sum(-1, keepdim=True)
    if head == "regression":
        return pred["y_hat"][..., :1]
    raise NotImplementedError(f"no closed-form mean for head {head}")


def issue_positions(ds, forecast_len: int, hours: set[int], stride_h: int) -> list[int]:
    """Dataset positions whose issue time (last hindcast step) is on the given UTC hours, thinned to one per stride."""
    out, last = [], None
    if hasattr(ds, "sample_dates"):
        dates = [t for _, _, t in ds.sample_dates()]
    else:  # stock NeuralHydrology datasets
        freq = ds.frequencies[0]
        dates = [pd.Timestamp(ds._dates[b][freq][idx[0] - forecast_len]) for b, idx in (ds.lookup_table[i] for i in range(len(ds)))]
    for i, t in enumerate(dates):
        if t.hour in hours and (last is None or (t - last) >= pd.Timedelta(hours=stride_h)):
            out.append(i)
            last = t
    return out


def _nse(sim: np.ndarray, obs: np.ndarray) -> float:
    ok = ~np.isnan(obs) & ~np.isnan(sim)
    if ok.sum() < 10:
        return np.nan
    o, s = obs[ok], sim[ok]
    denom = ((o - o.mean()) ** 2).sum()
    return float(1 - ((s - o) ** 2).sum() / denom) if denom > 0 else np.nan


def _kge(sim: np.ndarray, obs: np.ndarray) -> float:
    ok = ~np.isnan(obs) & ~np.isnan(sim)
    if ok.sum() < 10:
        return np.nan
    o, s = obs[ok], sim[ok]
    if o.std() == 0 or s.std() == 0 or o.mean() == 0:
        return np.nan
    r = np.corrcoef(o, s)[0, 1]
    return float(1 - np.sqrt((r - 1) ** 2 + (s.std() / o.std() - 1) ** 2 + (s.mean() / o.mean() - 1) ** 2))


class FlowcastValidator:
    def __init__(self, cfg: Config, scaler: dict, id_to_int: dict | None = None, issue_hours=(0,), stride_h: int = 24):
        self.cfg = cfg
        self.scaler = scaler
        self.id_to_int = id_to_int or {}
        self.hours = set(issue_hours)
        self.stride_h = stride_h
        self.L = cfg.forecast_seq_length or cfg.predict_last_n
        self.leads = [h for h in LEADS_H if h <= self.L]
        target = cfg.target_variables[0]
        self.center = float(scaler["xarray_feature_center"][target].values)
        self.scale = float(scaler["xarray_feature_scale"][target].values)
        self._cache: dict[str, tuple] = {}

    def _basin(self, basin: str):
        if basin not in self._cache:
            try:
                ds = get_dataset(self.cfg, is_train=False, period="validation", basin=basin, scaler=self.scaler, id_to_int=self.id_to_int)
                self._cache[basin] = (ds, issue_positions(ds, self.L, self.hours, self.stride_h))
            except NoEvaluationDataError:
                self._cache[basin] = (None, [])
        return self._cache[basin]

    def evaluate(self, model, loss_obj, device) -> dict[str, float]:
        model.eval()
        head = self.cfg.head.lower()
        losses, n_batches = [], 0
        per_basin: dict[str, dict[str, float]] = {}
        with torch.no_grad():
            for basin in load_basin_file(self.cfg.validation_basin_file):
                ds, positions = self._basin(basin)
                if not positions:
                    continue
                sims, obss = [], []
                for data in DataLoader(Subset(ds, positions), batch_size=self.cfg.batch_size, collate_fn=ds.collate_fn):
                    for key in data:
                        if key.startswith("x_d"):
                            data[key] = {k: v.to(device) for k, v in data[key].items()}
                        elif not key.startswith("date"):
                            data[key] = data[key].to(device)
                    data = model.pre_model_hook(data, is_train=False)
                    pred = model(data)
                    loss, _ = loss_obj(pred, data)
                    if not torch.isnan(loss):
                        losses.append(loss.item())
                    n_batches += 1
                    mean = mixture_mean(pred, head, self.cfg.n_distributions if head in ('cmal', 'gmm') else 1)[:, -self.L :, 0].cpu().numpy()
                    sims.append(mean)
                    obss.append(data["y"][:, -self.L :, 0].cpu().numpy())
                sim = np.clip(np.concatenate(sims) * self.scale + self.center, 0, None)
                obs = np.concatenate(obss) * self.scale + self.center
                per_basin[basin] = {}
                for h in self.leads:
                    per_basin[basin][f"NSE_{h}h"] = _nse(sim[:, h - 1], obs[:, h - 1])
                    per_basin[basin][f"KGE_{h}h"] = _kge(sim[:, h - 1], obs[:, h - 1])
        model.train()
        metrics = {"avg_total_loss": float(np.mean(losses)) if losses else np.nan}
        frame = pd.DataFrame(per_basin).T
        for col in frame.columns:
            metrics[col] = float(frame[col].median())
        metrics["basins"] = float(len(per_basin))
        return metrics
