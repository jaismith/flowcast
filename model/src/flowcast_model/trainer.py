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
* Optional mixed precision (`flowcast.train.amp`: auto | bf16 | fp16 | none): the model runs under autocast, the
  output heads and the loss stay in fp32, and fp16 uses a GradScaler.
* Optimizer steps with non-finite gradients are skipped. Those batches, and batches with a NaN loss, are logged
  with their basins and time windows (`events.jsonl`).
* Fine-tuning (`flowcast.train.init_from`: another run's directory, local or s3://): the run uses that run's feature
  scaler and starts from its weights (`init_epoch`: best | N). `select_until` limits the in-training validation
  (and so the best epoch) to issues up to a date, e.g. a year before the scored years.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path

import boto3
import numpy as np
import torch
from neuralhydrology.datautils.utils import load_scaler
from neuralhydrology.training.basetrainer import BaseTrainer
from neuralhydrology.training.earlystopper import EarlyStopper
from neuralhydrology.utils.config import Config
from torch.utils.data import DataLoader

from .dataset import BasinBlockBatchSampler, ZarrCubeDataset
from .models import apply_variants, elementwise_cmal_loss
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


def fetch_init(source: str, dest: Path, epoch: str | int = "best") -> Path:
    """Copy another run's feature scaler and chosen weights (run directory, local or s3://) into `dest`.

    `dest/train_data/train_data_scaler.yml` is laid out for `load_scaler(dest)`; returns the weights file.
    """
    def get(rel: str) -> Path:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.startswith("s3://"):
            bucket, _, prefix = source[5:].partition("/")
            boto3.client("s3").download_file(bucket, f"{prefix.rstrip('/')}/{rel}", str(target))
        else:
            shutil.copy(Path(source) / rel, target)
        return target

    get("train_data/train_data_scaler.yml")
    if epoch == "best":
        rows = [r for r in csv.DictReader(get("validation_metrics.csv").open()) if r.get("avg_total_loss") not in (None, "", "nan")]
        epoch = int(min(rows, key=lambda r: float(r["avg_total_loss"]))["epoch"])
    return get(f"model_epoch{int(epoch):03d}.pt")


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


def amp_dtype(setting: str | None, device: torch.device) -> torch.dtype | None:
    """Autocast dtype for `flowcast.train.amp` (none | auto | bf16 | fp16); auto = bf16 where supported, else fp16."""
    setting = (setting or "none").lower()
    if setting == "none" or device.type != "cuda":
        return None
    if setting == "auto":
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    return {"bf16": torch.bfloat16, "fp16": torch.float16}[setting]


def heads_in_fp32(model: torch.nn.Module) -> torch.nn.Module:
    """Run the output heads (CMAL scale softplus + floor) outside autocast, on fp32 inputs."""
    for name in ("hindcast_head", "forecast_head", "head"):
        head = getattr(model, name, None)
        if head is None:
            continue
        original = head.forward

        def forward(x, original=original):
            with torch.autocast(device_type="cuda", enabled=False):
                return original(x.float())

        head.forward = forward
    return model


class _LoaderWait:
    """A DataLoader that adds up the time spent waiting for its batches. Every step syncs on the loss, so this is
    about the time the GPU sits idle for want of data."""

    def __init__(self, loader):
        self.loader = loader
        self.wait_s = 0.0

    def __iter__(self):
        batches = iter(self.loader)
        while True:
            start = time.perf_counter()
            try:
                batch = next(batches)
            except StopIteration:
                return
            self.wait_s += time.perf_counter() - start
            yield batch

    def __len__(self):
        return len(self.loader)

    def __getattr__(self, name):
        return getattr(self.loader, name)


class FlowcastTrainer(BaseTrainer):
    def __init__(self, cfg: Config, on_checkpoint: Callable[[int], None] | None = None, model_options: dict | None = None, train_options: dict | None = None):
        self._on_checkpoint = on_checkpoint
        self._model_options = model_options or {}
        self._train_options = train_options or {}
        super().__init__(cfg)
        self._amp = amp_dtype(self._train_options.get("amp"), self.device)
        # Not `_scaler`: BaseTrainer keeps the feature normalization there and passes it to the datasets.
        self._grad_scaler = torch.amp.GradScaler("cuda") if self._amp == torch.float16 else None
        if self._amp is not None:
            LOGGER.info("mixed precision: autocast %s%s", self._amp, " with GradScaler" if self._grad_scaler else "")

    def _get_model(self):
        model = apply_variants(super()._get_model(), self._model_options)
        if (self._train_options.get("amp") or "none").lower() != "none":
            model = heads_in_fp32(model)
        return model

    def _train_epoch(self, epoch: int):
        loader, timed = self.loader, _LoaderWait(self.loader)
        self.loader = timed
        start = time.perf_counter()
        try:
            self._run_epoch(epoch)
        finally:
            self.loader = loader
        seconds = time.perf_counter() - start
        LOGGER.info("Epoch %d took %.0f s, %.0f s (%.0f%%) waiting for the data loader", epoch, seconds, timed.wait_s, 100 * timed.wait_s / max(seconds, 1e-9))
        if self.cfg.run_dir:
            log_event(self.cfg.run_dir, "epoch_time", epoch=epoch, seconds=round(seconds, 1), loader_wait_s=round(timed.wait_s, 1))

    def _run_epoch(self, epoch: int):
        if self._amp is None:
            return super()._train_epoch(epoch)
        self.model.train()
        self.experiment_logger.train()
        nan_count = 0
        for i, data in enumerate(self.loader):
            if self._max_updates_per_epoch is not None and i >= self._max_updates_per_epoch:
                break
            for key in data.keys():
                if key.startswith("x_d"):
                    data[key] = {k: v.to(self.device, non_blocking=True) for k, v in data[key].items()}
                elif not key.startswith("date"):
                    data[key] = data[key].to(self.device, non_blocking=True)
            data = self.model.pre_model_hook(data, is_train=True)
            with torch.autocast(device_type="cuda", dtype=self._amp):
                predictions = self.model(data)
            predictions = {k: v.float() if torch.is_tensor(v) and v.is_floating_point() else v for k, v in predictions.items()}
            loss, all_losses = self.loss_obj(predictions, data)  # fp32, outside autocast
            if torch.isnan(loss):
                nan_count += 1
                if nan_count > self._allow_subsequent_nan_losses:
                    raise RuntimeError(f"Loss was NaN for {nan_count} times in a row. Stopped training.")
                LOGGER.warning(f"Loss is Nan; ignoring step. (#{nan_count}/{self._allow_subsequent_nan_losses})")
            else:
                nan_count = 0
                self.optimizer.zero_grad()
                if self._grad_scaler is not None:
                    self._grad_scaler.scale(loss).backward()
                    self._grad_scaler.unscale_(self.optimizer)
                else:
                    loss.backward()
                if self.cfg.clip_gradient_norm is not None:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.cfg.clip_gradient_norm)
                if self._grad_scaler is not None:
                    self._grad_scaler.step(self.optimizer)
                    self._grad_scaler.update()
                else:
                    self.optimizer.step()
            self.experiment_logger.log_step(**{k: v.item() for k, v in all_losses.items()})

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
        init_weights = None
        if self._train_options.get("init_from"):
            # the other run's scaler on every start (a resumed run would otherwise recompute it from its own basins)
            init_dir = Path(self.cfg.run_dir) / "init"
            init_weights = fetch_init(str(self._train_options["init_from"]), init_dir, self._train_options.get("init_epoch", "best"))
            self._scaler = load_scaler(init_dir)
        super().initialize_training()
        if self._train_options.get("elementwise_mask"):
            elementwise_cmal_loss(self.loss_obj)
        if init_weights is not None:
            shutil.copy(Path(self.cfg.run_dir) / "init" / "train_data" / "train_data_scaler.yml", Path(self.cfg.train_dir) / "train_data_scaler.yml")
            if self._epoch == 0:
                self.model.load_state_dict(torch.load(init_weights, map_location=self.device))
                log_event(self.cfg.run_dir, "init_from", source=str(self._train_options["init_from"]), weights=init_weights.name)
        self._guard_optimizer()
        self._track_batches()
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

    def _guard_optimizer(self) -> None:
        """Skip optimizer steps with non-finite gradients.

        NeuralHydrology skips steps whose loss is NaN, but a finite loss can still produce an inf/NaN gradient; one
        such step turns every weight into NaN and every later loss with it (seen once in a 554-basin residual run).
        """
        optimizer = self.optimizer
        original_step = optimizer.step
        trainer = self

        def guarded_step(*args, **kwargs):
            for group in optimizer.param_groups:
                for p in group["params"]:
                    if p.grad is not None and not torch.isfinite(p.grad).all():
                        trainer._skipped_steps = getattr(trainer, "_skipped_steps", 0) + 1
                        LOGGER.warning("non-finite gradient; skipping optimizer step (%d so far)", trainer._skipped_steps)
                        FlowcastTrainer._report_bad_batch(trainer, "non_finite_gradient")
                        optimizer.zero_grad()
                        return None
            return original_step(*args, **kwargs)

        optimizer.step = guarded_step

    def _track_batches(self) -> None:
        """Keep a handle on the current training batch so skipped steps and NaN losses can name their basins."""
        original_hook = self.model.pre_model_hook
        original_loss = self.loss_obj.forward
        trainer = self

        def hook(data, is_train):
            if is_train:
                trainer._current_batch = data
            return original_hook(data, is_train)

        def loss(prediction, data):
            out = original_loss(prediction, data)
            if trainer.model.training and torch.isnan(out[0]):
                FlowcastTrainer._report_bad_batch(trainer, "nan_loss")
            return out

        self.model.pre_model_hook = hook
        self.loss_obj.forward = loss

    def _report_bad_batch(self, kind: str) -> None:
        data = getattr(self, "_current_batch", None)
        if not data or "basin_index" not in data:
            return
        basins = self.loader.dataset.lookup_table.basins
        idx, counts = np.unique(data["basin_index"].cpu().numpy(), return_counts=True)
        ends = data["date"][:, -1]
        info = {
            "epoch": getattr(self, "_current_epoch", None),
            "basins": {basins[i]: int(n) for i, n in zip(idx, counts)},
            "window_end": [str(np.min(ends))[:13], str(np.max(ends))[:13]],
        }
        LOGGER.warning("%s batch: %s", kind, json.dumps(info))
        log_event(self.cfg.run_dir, kind, **info)

    def _get_tester(self):
        return None  # built lazily in train_and_validate, once the scaler exists

    def _get_data_loader(self, ds) -> DataLoader:
        if not isinstance(ds, ZarrCubeDataset):
            return super()._get_data_loader(ds)
        workers = self.cfg.num_workers
        sampler = BasinBlockBatchSampler(ds.lookup_table, self.cfg.batch_size, ds.options.block_basins, seed=self.cfg.seed, chunk_samples=ds.options.chunk_samples)
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
            if hasattr(self.loader.batch_sampler, "set_epoch"):
                self.loader.batch_sampler.set_epoch(epoch)
            self._current_epoch = epoch
            self._train_epoch(epoch=epoch)
            avg = self.experiment_logger.summarise()
            LOGGER.info("Epoch %d average loss: %s", epoch, ", ".join(f"{k}: {v:.5f}" for k, v in avg.items()))
            if epoch % cfg.save_weights_every == 0 or epoch == cfg.epochs:
                self._save_weights_and_optimizer(epoch)
                self._after_checkpoint(epoch)
            if cfg.validate_every and (epoch % cfg.validate_every == 0 or epoch == cfg.epochs):
                if self.validator is None:
                    self.validator = FlowcastValidator(cfg, self.loader.dataset.scaler, getattr(self.loader.dataset, "id_to_int", {}), until=self._train_options.get("select_until"))
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


def train(cfg: Config, on_checkpoint: Callable[[int], None] | None = None, model_options: dict | None = None, train_options: dict | None = None) -> Path:
    if cfg.head.lower() not in ["regression", "gmm", "umal", "cmal", ""]:
        raise ValueError(f"Unknown head {cfg.head}.")
    run_dir = Path(cfg.run_dir)
    done = latest_checkpoint(run_dir)
    if done >= cfg.epochs:
        if not (run_dir / "config.yml").exists():
            cfg.dump_config(run_dir)
        LOGGER.info("Run already trained to epoch %d", done)
        return run_dir
    trainer = FlowcastTrainer(cfg, on_checkpoint=on_checkpoint, model_options=model_options, train_options=train_options)
    trainer.initialize_training()
    trainer.train_and_validate()
    return Path(cfg.run_dir)
