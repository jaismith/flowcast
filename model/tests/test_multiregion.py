"""Pieces of the East + West dilution test: forecasts from the store that holds a basin, separate evaluation basins,
and fine-tunes whose untuned weights compete as epoch 0 (with early stopping that survives restarts)."""

import csv

import numpy as np
import pandas as pd
import torch
import xarray as xr
import yaml
from neuralhydrology.training.earlystopper import EarlyStopper
from neuralhydrology.utils.config import Config

from flowcast_model.cli import main
from flowcast_model.config import EVAL_BASIN_FILE, load_run, prepare_run
from flowcast_model.cube import Cube
from flowcast_model.hindcast import choose_epoch
from flowcast_model.trainer import latest_checkpoint, replay_early_stopping

from .test_pipeline import tiny_config

EPOCH = pd.Timestamp("2000-01-01")


def _store(path, basins: list[str], offset: float, n_init: int = 6):
    init = pd.date_range("2020-10-01", periods=n_init, freq="6h")
    lead = np.arange(1, 4, dtype=np.float32)
    time = pd.date_range("2020-09-01", "2020-10-03", freq="h")
    hours = lambda t: ((t - EPOCH) / pd.Timedelta(hours=1)).to_numpy().astype(np.int64)  # noqa: E731
    nb = len(basins)
    ds = xr.Dataset(
        {
            "qobs_mm_h": (("basin", "time"), np.full((nb, len(time)), offset, dtype=np.float32)),
            "gefs_precip_mm_h": (("basin", "gefs_init", "gefs_member", "gefs_lead"), (offset + np.arange(nb)[:, None, None, None] + np.zeros((1, n_init, 2, len(lead)))).astype(np.float32)),
            "area_km2": (("basin",), np.full(nb, 100.0, dtype=np.float32)),
        },
        coords={
            "basin": np.array(basins),
            "time": ("time", hours(time), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "gefs_init": ("gefs_init", hours(init), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "gefs_lead": lead,
            "gefs_member": np.arange(2, dtype=np.int8),
        },
    )
    ds.to_zarr(path, zarr_format=3, consolidated=False)
    return path


def test_forecasts_come_from_the_store_holding_the_basin(tmp_path):
    east = _store(tmp_path / "east.zarr", ["01000001", "01000002"], 100.0)
    west = _store(tmp_path / "west.zarr", ["09000001", "09000002"], 900.0)
    cube = Cube([east, west])
    start, end = pd.Timestamp("2020-10-01"), pd.Timestamp("2020-10-02")
    for basin, value in (("01000002", 101.0), ("09000001", 900.0), ("09000002", 901.0)):
        issues, leads, values, names = cube.load_forecast(basin, ["gefs_precip_mm_h"], start, end)["gefs_init"]
        assert names == ["gefs_precip_mm_h"] and len(issues) == 5
        np.testing.assert_array_equal(values, value)
    assert set(cube.basins) == {"01000001", "01000002", "09000001", "09000002"}
    df = cube.load_dynamic("09000002", ["qobs_mm_h"], start, end)
    assert (df["qobs_mm_h"] == 900.0).all()


def test_eval_basins_set_validation_and_hindcast_basins(tmp_path, cube_path):
    raw = yaml.safe_load(open(tiny_config(tmp_path, cube_path)))
    raw["flowcast"]["basins"] = ["01000001", "01000002"]
    raw["flowcast"]["eval_basins"] = ["01000002", "01000003"]
    cfg, options = prepare_run(raw, tmp_path / "run")
    assert (tmp_path / "run" / "basins.txt").read_text().split() == ["01000001", "01000002"]
    assert cfg.validation_basin_file.name == EVAL_BASIN_FILE == cfg.test_basin_file.name
    assert cfg.validation_basin_file.read_text().split() == ["01000002", "01000003"]
    assert options.eval_basins == ["01000002", "01000003"]

    # without eval_basins the three files are one
    raw["flowcast"].pop("eval_basins")
    cfg, options = prepare_run(raw, tmp_path / "run2")
    assert cfg.validation_basin_file == cfg.train_basin_file and options.eval_basins is None


def test_eval_basins_survive_a_moved_run(tmp_path, cube_path):
    raw = yaml.safe_load(open(tiny_config(tmp_path, cube_path)))
    raw["flowcast"]["eval_basins"] = ["01000003"]
    config = tmp_path / "eval.yml"
    config.write_text(yaml.safe_dump(raw))
    main(["train", "--config", str(config), "--run-dir", str(tmp_path / "run"), "--set", "epochs=1"])
    (tmp_path / "run").rename(tmp_path / "moved")
    cfg, _ = load_run(tmp_path / "moved")
    assert cfg.validation_basin_file == tmp_path / "moved" / EVAL_BASIN_FILE
    assert cfg.train_basin_file == tmp_path / "moved" / "basins.txt"


def _metrics(path, rows: list[tuple[int, float]]) -> None:
    with (path / "validation_metrics.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["epoch", "avg_total_loss"])
        w.writerows(rows)


def test_replay_early_stopping_counts_from_epoch_zero(tmp_path):
    # the untuned weights (epoch 0) are never beaten: patience 2 stops at epoch 2
    _metrics(tmp_path, [(0, 0.5), (1, 0.6), (2, 0.55), (3, 0.4)])
    assert replay_early_stopping(EarlyStopper(2, 0.0001), tmp_path, 3, 0) == 2
    _metrics(tmp_path, [(1, 1.0), (2, 0.9), (3, 0.95), (4, 0.91), (5, 0.8)])
    assert replay_early_stopping(EarlyStopper(2, 0.0001), tmp_path, 5, 0) == 4
    stopper = EarlyStopper(2, 0.0001)
    assert replay_early_stopping(stopper, tmp_path, 3, 0) is None
    assert stopper.check_early_stopping(0.91)
    assert replay_early_stopping(EarlyStopper(2, 0.0001), tmp_path / "missing", 5, 0) is None


def test_early_stopping_keeps_the_learning_rate_schedule():
    cfg = Config({"early_stopping": True, "validate_every": 1, "patience_early_stopping": 2})
    assert cfg.early_stopping and not cfg.dynamic_learning_rate
    assert Config({"dynamic_learning_rate": True}).dynamic_learning_rate


def test_choose_epoch_can_pick_the_untuned_weights(tmp_path):
    for e in (0, 1, 2):
        (tmp_path / f"model_epoch{e:03d}.pt").write_bytes(b"")
    for e in (1, 2):
        (tmp_path / f"optimizer_state_epoch{e:03d}.pt").write_bytes(b"")
    _metrics(tmp_path, [(0, 0.3), (1, 0.5), (2, 0.4)])
    assert choose_epoch(tmp_path, "best") == 0
    assert choose_epoch(tmp_path, "last") == 2


def test_fine_tune_validates_the_initial_weights_as_epoch_zero(tmp_path, cube_path):
    base = tmp_path / "base"
    main(["train", "--config", tiny_config(tmp_path, cube_path), "--run-dir", str(base)])
    raw = yaml.safe_load(open(tiny_config(tmp_path, cube_path, early_stopping=True, patience_early_stopping=1, minimum_epochs_before_early_stopping=0, learning_rate=0.0)))
    raw["flowcast"]["train"] = {"init_from": str(base), "init_epoch": 2, "validate_init": True}
    config = tmp_path / "ft.yml"
    config.write_text(yaml.safe_dump(raw))
    tuned = tmp_path / "tuned"
    main(["train", "--config", str(config), "--run-dir", str(tuned), "--set", "epochs=4"])

    ref = torch.load(base / "model_epoch002.pt")
    epoch0 = torch.load(tuned / "model_epoch000.pt")
    assert all(torch.equal(v, ref[k]) for k, v in epoch0.items())
    rows = list(csv.DictReader((tuned / "validation_metrics.csv").open()))
    assert [int(r["epoch"]) for r in rows][0] == 0
    # epoch 0 is a candidate but not a resumable checkpoint
    assert latest_checkpoint(tuned) >= 1 and not (tuned / "optimizer_state_epoch000.pt").exists()
    # a learning rate of 0 can't beat the start, so patience 1 stops after epoch 1 and a relaunch counts it as trained
    assert latest_checkpoint(tuned) == 1
    main(["train", "--config", str(config), "--run-dir", str(tuned), "--set", "epochs=4"])
    assert latest_checkpoint(tuned) == 1
