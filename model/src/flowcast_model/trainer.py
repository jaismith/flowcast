"""Resumable NeuralHydrology trainer for Spot instances.

Differences from `neuralhydrology.training.basetrainer.BaseTrainer`:

* The run directory is fixed (no timestamped folder). If it already holds a checkpoint, training resumes from the
  latest epoch whose model and optimizer state both exist, and runs until `epochs` in total (NeuralHydrology's own
  `continue_training` nests a new folder and adds `epochs` more on every resume).
* `flowcast_zarr` datasets are served through `BasinBlockBatchSampler` with persistent, pinned-memory workers.
* After every checkpoint `on_checkpoint(epoch)` runs (the job uses it to sync the run to S3), and validation
  metrics are appended to `validation_metrics.csv`, which also decides the `best` epoch for hindcasts.
* A `STOP` file in the run directory ends training cleanly after the current epoch (used by the max-runtime
  watchdog before its hard deadline).
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path

import numpy as np
import torch
from neuralhydrology.training.basetrainer import BaseTrainer
from neuralhydrology.training.earlystopper import EarlyStopper
from neuralhydrology.utils.config import Config
from torch.utils.data import DataLoader

from .dataset import BasinBlockBatchSampler, ZarrCubeDataset
from .models import apply_variants
from .validation import FlowcastValidator

LOGGER = logging.getLogger(__name__)
CHECKPOINT_RE = re.compile(r"model_epoch(\d{3})\.pt$")


def log_event(run_dir: Path, event: str, **fields) -> None:
    with (Path(run_dir) / "events.jsonl").open("a") as fp:
        fp.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}) + "\n")


def latest_checkpoint(run_dir: Path) -> int:
    """Latest epoch with both model and optimizer state saved (0 if none)."""
    epochs = []
    for p in Path(run_dir).glob("model_epoch*.pt"):
        m = CHECKPOINT_RE.search(p.name)
        if m and (p.parent / f"optimizer_state_epoch{m.group(1)}.pt").exists():
            epochs.append(int(m.group(1)))
    return max(epochs, default=0)


def best_epoch(run_dir: Path, metric: str = "avg_total_loss") -> int | None:
    path = Path(run_dir) / "validation_metrics.csv"
    if not path.exists():
        return None
    rows = list(csv.DictReader(path.open()))
    rows = [r for r in rows if r.get(metric) not in (None, "", "nan") and (Path(run_dir) / f"model_epoch{int(r['epoch']):03d}.pt").exists()]
    if not rows:
        return None
    higher_better = metric.lower() in {"nse", "kge"}
    key = (lambda r: -float(r[metric])) if higher_better else (lambda r: float(r[metric]))
    return int(min(rows, key=key)["epoch"])


class FlowcastTrainer(BaseTrainer):
    def __init__(self, cfg: Config, on_checkpoint: Callable[[int], None] | None = None, model_options: dict | None = None):
        self._on_checkpoint = on_checkpoint
        self._model_options = model_options or {}
        super().__init__(cfg)

    def _get_model(self):
        return apply_variants(super()._get_model(), self._model_options)

    # ------------------------------------------------------------------ run directory / resume

    def _get_start_epoch_number(self):
        return latest_checkpoint(Path(self.cfg.run_dir)) if self.cfg.run_dir else 0

    def _create_folder_structure(self):
        run_dir = Path(self.cfg.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.train_dir = run_dir / "train_data"
        self.cfg.train_dir.mkdir(exist_ok=True)
        if self.cfg.log_n_figures is not None:
            self.cfg.img_log_dir = run_dir / "img_log"
            self.cfg.img_log_dir.mkdir(exist_ok=True)
        # the NH logger refuses to overwrite config.yml; on resume it is re-dumped unchanged
        (run_dir / "config.yml").unlink(missing_ok=True)

    def initialize_training(self):
        if self.device.type == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            torch.backends.cudnn.benchmark = True
        super().initialize_training()
        log_event(self.cfg.run_dir, "resume" if self._epoch > 0 else "start", epoch=self._epoch, device=str(self.device), samples=len(self.loader.dataset))
        if self._epoch > 0:
            run_dir = Path(self.cfg.run_dir)
            LOGGER.info("### Resuming from epoch %d", self._epoch)
            self.model.load_state_dict(torch.load(run_dir / f"model_epoch{self._epoch:03d}.pt", map_location=self.device))
            self.optimizer.load_state_dict(torch.load(run_dir / f"optimizer_state_epoch{self._epoch:03d}.pt", map_location=self.device))
            rng = run_dir / f"rng_state_epoch{self._epoch:03d}.pt"
            if rng.exists():
                state = torch.load(rng, weights_only=False)
                torch.set_rng_state(state["torch"])
                np.random.set_state(state["numpy"])
            self.experiment_logger.epoch = self._epoch
            self.experiment_logger.update = len(self.loader) * self._epoch

    def _get_tester(self):
        return None  # built lazily in train_and_validate, once the scaler exists

    def _get_data_loader(self, ds) -> DataLoader:
        if not isinstance(ds, ZarrCubeDataset):
            return super()._get_data_loader(ds)
        workers = self.cfg.num_workers
        sampler = BasinBlockBatchSampler(ds.lookup_table, self.cfg.batch_size, ds.options.block_basins, seed=self.cfg.seed)
        return DataLoader(
            ds,
            batch_sampler=sampler,
            num_workers=workers,
            collate_fn=ds.collate_fn,
            persistent_workers=workers > 0,
            pin_memory=torch.cuda.is_available(),
            prefetch_factor=4 if workers > 0 else None,
        )

    def _save_weights_and_optimizer(self, epoch: int):
        super()._save_weights_and_optimizer(epoch)
        run_dir = Path(self.cfg.run_dir)
        torch.save({"torch": torch.get_rng_state(), "numpy": np.random.get_state()}, run_dir / f"rng_state_epoch{epoch:03d}.pt")
        tmp = run_dir / "checkpoint.json.tmp"
        tmp.write_text(json.dumps({"epoch": epoch, "epochs": self.cfg.epochs}))
        tmp.replace(run_dir / "checkpoint.json")
        log_event(run_dir, "checkpoint", epoch=epoch)

    # ------------------------------------------------------------------ loop

    def _log_validation(self, epoch: int, metrics: dict) -> None:
        path = Path(self.cfg.run_dir) / "validation_metrics.csv"
        row = {"epoch": epoch, **{k: float(v) for k, v in metrics.items()}}
        rows = list(csv.DictReader(path.open())) if path.exists() else []
        rows = [r for r in rows if int(r["epoch"]) != epoch] + [row]
        fields = sorted({k for r in rows for k in r}, key=lambda k: (k != "epoch", k))
        with path.open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def train_and_validate(self):
        cfg = self.cfg
        run_dir = Path(cfg.run_dir)
        stopper = EarlyStopper(patience=self._patience_early_stopping, min_delta=0.0001) if self._early_stopping else None
        scheduler = None
        if self._dynamic_learning_rate:
            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode="min", factor=self._factor_dynamic_learning_rate, patience=self._patience_dynamic_learning_rate)
        for epoch in range(self._epoch + 1, cfg.epochs + 1):
            if not self._dynamic_learning_rate:
                lr = max((e for e in cfg.learning_rate if e <= epoch), default=None)
                if lr is not None:
                    for group in self.optimizer.param_groups:
                        group["lr"] = cfg.learning_rate[lr]
            self._train_epoch(epoch=epoch)
            avg = self.experiment_logger.summarise()
            LOGGER.info("Epoch %d average loss: %s", epoch, ", ".join(f"{k}: {v:.5f}" for k, v in avg.items()))
            if epoch % cfg.save_weights_every == 0 or epoch == cfg.epochs:
                self._save_weights_and_optimizer(epoch)
                self._after_checkpoint(epoch)
            if cfg.validate_every and (epoch % cfg.validate_every == 0 or epoch == cfg.epochs):
                if self.validator is None:
                    self.validator = FlowcastValidator(cfg, self.loader.dataset.scaler, getattr(self.loader.dataset, "id_to_int", {}))
                valid = self.validator.evaluate(self.model, self.loss_obj, self.device)
                LOGGER.info("Epoch %d validation: %s", epoch, ", ".join(f"{k}: {v:.5f}" for k, v in valid.items()))
                self._log_validation(epoch, valid)
                if stopper is not None and epoch > self._minimum_epochs_before_early_stopping and stopper.check_early_stopping(valid["avg_total_loss"]):
                    LOGGER.info("Early stopping at epoch %d", epoch)
                    self._after_checkpoint(epoch)
                    break
                if scheduler is not None:
                    scheduler.step(valid["avg_total_loss"])
            self._after_checkpoint(epoch)
            if (run_dir / "STOP").exists():
                LOGGER.warning("STOP file found; ending training after epoch %d", epoch)
                break
        log_event(run_dir, "finished", epoch=latest_checkpoint(run_dir))
        if cfg.log_tensorboard:
            self.experiment_logger.stop_tb()

    def _after_checkpoint(self, epoch: int) -> None:
        if self._on_checkpoint is not None:
            try:
                self._on_checkpoint(epoch)
            except Exception:  # a failed sync must not kill training; the next epoch retries
                LOGGER.exception("checkpoint callback failed")


def train(cfg: Config, on_checkpoint: Callable[[int], None] | None = None, model_options: dict | None = None) -> Path:
    if cfg.head.lower() not in ["regression", "gmm", "umal", "cmal", ""]:
        raise ValueError(f"Unknown head {cfg.head}.")
    run_dir = Path(cfg.run_dir)
    done = latest_checkpoint(run_dir)
    if done >= cfg.epochs:
        if not (run_dir / "config.yml").exists():
            cfg.dump_config(run_dir)
        LOGGER.info("Run already trained to epoch %d", done)
        return run_dir
    trainer = FlowcastTrainer(cfg, on_checkpoint=on_checkpoint, model_options=model_options)
    trainer.initialize_training()
    trainer.train_and_validate()
    return Path(cfg.run_dir)
