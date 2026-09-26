"""Acceptance tests for the streaming dataset: sample-for-sample equality with NeuralHydrology's stock loader."""

import numpy as np
import pandas as pd
import pytest
import torch
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.utils.config import Config

from flowcast_model.cube import Cube, FrozenTestError
from flowcast_model.dataset import BasinBlockBatchSampler, DatasetOptions, ZarrCubeDataset
from flowcast_model.stock import export_generic

from .conftest import BASINS


def base_cfg(tmp_path, **kw) -> dict:
    basin_file = tmp_path / "basins.txt"
    basin_file.write_text("\n".join(BASINS) + "\n")
    cfg = dict(
        experiment_name="t",
        run_dir=str(tmp_path / "run"),
        train_basin_file=str(basin_file),
        validation_basin_file=str(basin_file),
        test_basin_file=str(basin_file),
        train_start_date="01/10/2015",
        train_end_date="30/09/2018",
        validation_start_date="01/10/2018",
        validation_end_date="30/09/2019",
        test_start_date="01/10/2018",
        test_end_date="30/09/2019",
        dynamic_inputs=["precip", "temp", "qobs_shift1"],
        lagged_features={"qobs": 1},
        target_variables=["qobs"],
        static_attributes=["area_km2", "elev"],
        seq_length=48,
        predict_last_n=1,
        model="cudalstm",
        hidden_size=8,
        loss="nse",
        head="regression",
        optimizer="Adam",
        learning_rate=0.001,
        batch_size=16,
        epochs=1,
        device="cpu",
        verbose=0,
    )
    cfg.update(kw)
    return cfg


FORECAST = dict(
    model="handoff_forecast_lstm",
    dynamic_inputs=["precip", "temp", "qobs_shift1"],
    hindcast_inputs=["precip", "temp", "qobs_shift1"],
    forecast_inputs=["precip", "temp"],
    seq_length=72,
    forecast_seq_length=24,
    predict_last_n=24,
    hindcast_hidden_size=8,
    forecast_hidden_size=8,
    state_handoff_network={"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
)


def make_pair(tmp_path, cube_path, overrides=None, period="train", options=None):
    overrides = {k: v for k, v in (overrides or {}).items()}
    raw = base_cfg(tmp_path, **overrides)
    raw = {k: v for k, v in raw.items() if v is not None}
    generic_dir = export_generic(Cube([cube_path]), tmp_path / "generic", BASINS, ["precip", "temp", "qobs"], ["area_km2", "elev"], "2014-10-01", "2019-09-30T23:00")
    stock_cfg = Config({**raw, "dataset": "generic", "data_dir": str(generic_dir)})
    ours_cfg = Config({**raw, "dataset": "flowcast_zarr", "data_dir": str(cube_path)})
    for cfg, name in ((stock_cfg, "stock"), (ours_cfg, "ours")):
        cfg.train_dir = tmp_path / name / "train_data"
        cfg.train_dir.mkdir(parents=True)
    ZarrCubeDataset.configure(options or DatasetOptions(block_basins=2))
    stock = get_dataset(stock_cfg, is_train=True, period="train")
    ours = get_dataset(ours_cfg, is_train=True, period="train")
    if period != "train":
        stock = get_dataset(stock_cfg, is_train=False, period=period, scaler=stock.scaler, basin=BASINS[1])
        ours = get_dataset(ours_cfg, is_train=False, period=period, scaler=ours.scaler, basin=BASINS[1])
    return stock, ours


def assert_same_sample(a: dict, b: dict):
    assert set(a) == set(b)
    for key in a:
        if isinstance(a[key], dict):
            assert set(a[key]) == set(b[key]), key
            for k in a[key]:
                np.testing.assert_allclose(a[key][k].numpy(), b[key][k].numpy(), rtol=1e-5, atol=1e-5, err_msg=f"{key}/{k}")
        elif isinstance(a[key], torch.Tensor):
            np.testing.assert_allclose(a[key].numpy(), b[key].numpy(), rtol=1e-5, atol=1e-5, err_msg=key)
        else:
            np.testing.assert_array_equal(a[key], b[key])


def check_equal(stock, ours, stride=1):
    assert len(stock) == len(ours)
    for name in ("xarray_feature_center", "xarray_feature_scale"):
        for var in stock.scaler[name].data_vars:
            np.testing.assert_allclose(float(stock.scaler[name][var]), float(ours.scaler[name][var]), rtol=1e-5)
    pd.testing.assert_series_equal(stock.scaler["attribute_means"], ours.scaler["attribute_means"])
    for i in range(0, len(stock), stride):
        assert stock.lookup_table[i][0] == ours.lookup_table[i][0]
        assert list(stock.lookup_table[i][1]) == list(ours.lookup_table[i][1])
        assert_same_sample(stock[i], ours[i])


def test_matches_stock_loader_train(tmp_path, cube_path):
    stock, ours = make_pair(tmp_path, cube_path)
    check_equal(stock, ours, stride=7)


def test_matches_stock_loader_forecast_mode(tmp_path, cube_path):
    stock, ours = make_pair(tmp_path, cube_path, FORECAST)
    check_equal(stock, ours, stride=11)
    assert ours[0]["x_d_forecast"]["precip"].shape == (24, 1)


def test_matches_stock_loader_validation(tmp_path, cube_path):
    stock, ours = make_pair(tmp_path, cube_path, FORECAST, period="validation")
    check_equal(stock, ours, stride=5)


def test_optional_inputs_keep_samples_with_missing_lagged_flow(tmp_path, cube_path):
    groups = dict(FORECAST, hindcast_inputs=[["precip", "temp"], ["qobs_shift1"]], forecast_inputs=[["precip", "temp"]], nan_handling_method="masked_mean")
    stock, ours = make_pair(tmp_path, cube_path, groups, options=DatasetOptions(optional_inputs=["qobs_shift1"], group_dropout={"qobs_shift1": 1.0}))
    assert len(ours) > len(stock)
    sample = ours[0]
    assert torch.isnan(sample["x_d_hindcast"]["qobs_shift1"]).all()
    assert not torch.isnan(sample["x_d_hindcast"]["precip"]).any()


def test_block_sampler_covers_every_sample_once(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path)
    sampler = BasinBlockBatchSampler(ours.lookup_table, batch_size=64, block_basins=2, seed=1)
    batches = list(sampler)
    assert len(batches) == len(sampler)
    flat = np.concatenate(batches)
    assert sorted(flat.tolist()) == list(range(len(ours)))
    assert all(len(b) == 64 for b in batches[:-1])
    offsets = ours.lookup_table.offsets
    basins_per_batch = [len(set(np.searchsorted(offsets, b, side="right") - 1)) for b in batches]
    assert max(basins_per_batch) <= 3  # a block of 2, plus carry-over from the previous block


def test_basin_cache_is_bounded(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path, options=DatasetOptions(block_basins=1, cache_basins=1))
    for i in range(0, len(ours), max(1, len(ours) // 20)):
        ours[i]
        assert len(ours._blocks) <= 1


def test_frozen_test_guard(cube_path):
    cube = Cube([cube_path])
    with pytest.raises(FrozenTestError):
        cube.load_dynamic("01000001", ["qobs"], pd.Timestamp("2022-09-01"), pd.Timestamp("2022-10-02"))
