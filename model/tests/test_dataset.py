"""Acceptance tests for the streaming dataset: sample-for-sample equality with NeuralHydrology's stock loader."""

import numpy as np
import pandas as pd
import pytest
import torch
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.utils.config import Config

from flowcast_model.cube import Cube, FrozenTestError
from flowcast_model import index_cache
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
        train_start_date="01/10/2018",
        train_end_date="30/09/2020",
        validation_start_date="01/10/2020",
        validation_end_date="30/09/2021",
        test_start_date="01/10/2020",
        test_end_date="30/09/2021",
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
    generic_dir = export_generic(Cube([cube_path]), tmp_path / "generic", BASINS, ["precip", "temp", "qobs"], ["area_km2", "elev"], "2017-10-01", "2022-09-30T23:00")
    stock_cfg = Config({**raw, "dataset": "generic", "data_dir": str(generic_dir)})
    ours_cfg = Config({**raw, "dataset": "flowcast_zarr", "data_dir": str(cube_path)})
    for cfg, name in ((stock_cfg, "stock"), (ours_cfg, "ours")):
        cfg.train_dir = tmp_path / name / "train_data"
        cfg.train_dir.mkdir(parents=True)
    ZarrCubeDataset.configure(options or DatasetOptions(block_basins=2))
    stock = get_dataset(stock_cfg, is_train=True, period="train", scaler={})
    ours = get_dataset(ours_cfg, is_train=True, period="train", scaler={})
    if period != "train":
        stock = get_dataset(stock_cfg, is_train=False, period=period, scaler=stock.scaler, basin=BASINS[1])
        ours = get_dataset(ours_cfg, is_train=False, period=period, scaler=ours.scaler, basin=BASINS[1])
    return stock, ours


def assert_same_sample(a: dict, b: dict):
    assert set(a) == set(b) - {"basin_index"}  # training bookkeeping, ignored by the model
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


def test_chunked_block_sampler_mixes_basins_and_covers_every_sample_once(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path)
    offsets = ours.lookup_table.offsets
    n_basins = len(ours.lookup_table.basins)
    chunk = int(ours.lookup_table.counts.min()) // 4
    whole = BasinBlockBatchSampler(ours.lookup_table, batch_size=16, block_basins=1, seed=3)
    mixed = BasinBlockBatchSampler(ours.lookup_table, batch_size=16, block_basins=2, seed=3, chunk_samples=chunk)
    batches = list(mixed)
    assert sorted(np.concatenate(batches).tolist()) == list(range(len(ours)))
    assert all(len(b) == 16 for b in batches[:-1])

    def basins_seen(sampler, n):
        return len({int(b) for batch in list(sampler)[:n] for b in np.searchsorted(offsets, batch, side="right") - 1})

    n = max(1, chunk // 16)
    assert basins_seen(whole, 4 * n) == 1
    assert 1 < basins_seen(mixed, 4 * n) <= n_basins
    mixed.set_epoch(5)
    again = BasinBlockBatchSampler(ours.lookup_table, batch_size=16, block_basins=2, seed=3, chunk_samples=chunk)
    again.set_epoch(5)
    assert list(mixed) == list(again)


def test_block_sampler_order_depends_only_on_seed_and_epoch(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path)
    run = BasinBlockBatchSampler(ours.lookup_table, batch_size=64, block_basins=2, seed=1)
    run.set_epoch(1)
    first = list(run)
    run.set_epoch(2)
    second = list(run)
    resumed = BasinBlockBatchSampler(ours.lookup_table, batch_size=64, block_basins=2, seed=1)
    resumed.set_epoch(2)
    assert list(resumed) == second
    assert first != second
    assert ours[0]["basin_index"].item() == 0


def test_basin_cache_is_bounded(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path, options=DatasetOptions(block_basins=1, cache_basins=1))
    for i in range(0, len(ours), max(1, len(ours) // 20)):
        ours[i]
        assert len(ours._blocks) <= 1


def test_indexing_logs_progress_every_minute(tmp_path, cube_path, monkeypatch, caplog):
    monkeypatch.setattr("flowcast_model.dataset.INDEX_LOG_INTERVAL_S", 0)
    with caplog.at_level("INFO", logger="flowcast_model.dataset"):
        make_pair(tmp_path, cube_path)
    assert any(r.getMessage().startswith("indexed 1 of ") for r in caplog.records)


def test_basin_cache_default_fits_a_chunked_block(tmp_path, cube_path):
    (tmp_path / "chunked").mkdir()
    (tmp_path / "whole").mkdir()
    _, chunked = make_pair(tmp_path / "chunked", cube_path, options=DatasetOptions(block_basins=2, chunk_samples=16))
    assert chunked._blocks.capacity == 10
    _, whole = make_pair(tmp_path / "whole", cube_path, options=DatasetOptions(block_basins=2, chunk_samples=None))
    assert whole._blocks.capacity == 4


def test_frozen_test_guard(cube_path):
    cube = Cube([cube_path])
    with pytest.raises(FrozenTestError):
        cube.load_dynamic("01000001", ["qobs"], pd.Timestamp("2022-09-01"), pd.Timestamp("2022-10-02"))


def build_ours(tmp_path, cube_path, name, overrides=None, period="train", scaler=None, options=None):
    raw = {k: v for k, v in base_cfg(tmp_path, **(overrides or {})).items() if v is not None}
    cfg = Config({**raw, "dataset": "flowcast_zarr", "data_dir": str(cube_path)})
    cfg.train_dir = tmp_path / name / "train_data"
    cfg.train_dir.mkdir(parents=True, exist_ok=True)
    ZarrCubeDataset.configure(options or DatasetOptions(block_basins=2))
    return get_dataset(cfg, is_train=period == "train", period=period, scaler=scaler or {})


def assert_same_index(a, b, samples=True):
    assert a.lookup_table.basins == b.lookup_table.basins
    for va, vb in zip(a.lookup_table.valid, b.lookup_table.valid):
        np.testing.assert_array_equal(va, vb)
    for name in ("xarray_feature_center", "xarray_feature_scale"):
        assert set(a.scaler[name].data_vars) == set(b.scaler[name].data_vars)
        for var in a.scaler[name].data_vars:
            assert float(a.scaler[name][var]) == float(b.scaler[name][var]), var
    assert a._per_basin_target_stds.keys() == b._per_basin_target_stds.keys()
    for k in a._per_basin_target_stds:
        assert torch.equal(a._per_basin_target_stds[k], b._per_basin_target_stds[k])
    assert a.period_starts == b.period_starts
    if samples:
        for i in range(0, len(a), max(1, len(a) // 25)):
            assert_same_sample({k: v for k, v in a[i].items() if k != "basin_index"}, b[i])


@pytest.mark.parametrize("overrides", [{}, FORECAST])
def test_index_cache_matches_rebuilt_index(tmp_path, cube_path, caplog, overrides):
    cache_dir = tmp_path / "run" / "index_cache"
    built = build_ours(tmp_path, cube_path, "built", overrides)
    assert len(list(cache_dir.glob("train-*.npz"))) == 1
    with caplog.at_level("INFO", logger="flowcast_model.dataset"):
        cached = build_ours(tmp_path, cube_path, "cached", overrides)
    assert any("from the index cache" in r.getMessage() for r in caplog.records)
    assert_same_index(built, cached)
    # Evaluation datasets over several basins cache their period starts too (with the training scaler passed in).
    ev_built = build_ours(tmp_path, cube_path, "ev1", overrides, period="validation", scaler=built.scaler)
    ev_cached = build_ours(tmp_path, cube_path, "ev2", overrides, period="validation", scaler=built.scaler)
    assert len(list(cache_dir.glob("validation-*.npz"))) == 1
    assert ev_built.period_starts
    assert_same_index(ev_built, ev_cached)


def test_index_cache_rebuilds_on_any_mismatch_or_bad_file(tmp_path, cube_path, caplog):
    cache_dir = tmp_path / "run" / "index_cache"
    reference = build_ours(tmp_path, cube_path, "ref")
    (path,) = cache_dir.glob("train-*.npz")
    # A config change that affects sampling gets its own key.
    longer = build_ours(tmp_path, cube_path, "longer", {"seq_length": 60})
    assert len(list(cache_dir.glob("train-*.npz"))) == 2
    assert len(longer) != len(reference)
    # A different basin list too.
    two = tmp_path / "two.txt"
    two.write_text("\n".join(BASINS[:2]) + "\n")
    assert len(build_ours(tmp_path, cube_path, "two", {"train_basin_file": str(two)}).lookup_table.basins) == 2
    assert len(list(cache_dir.glob("train-*.npz"))) == 3
    # Options that only size blocks and caches share the cache.
    with caplog.at_level("INFO", logger="flowcast_model.dataset"):
        build_ours(tmp_path, cube_path, "blocks", options=DatasetOptions(block_basins=3, chunk_samples=64))
    assert any("from the index cache" in r.getMessage() for r in caplog.records)
    # A corrupted file is rebuilt, with the same result.
    path.write_bytes(b"not an npz")
    caplog.clear()
    with caplog.at_level("WARNING", logger="flowcast_model.index_cache"):
        rebuilt = build_ours(tmp_path, cube_path, "rebuilt")
    assert any("rebuilding" in r.getMessage() for r in caplog.records)
    assert_same_index(reference, rebuilt, samples=False)
    # A store whose arrays changed on disk (or whose metadata changed) gets a new key.
    key = index_cache.cache_key(reference)
    shard = next(p for p in (cube_path / "precip").rglob("*") if p.is_file())
    shard.write_bytes(shard.read_bytes() + b"\0")
    assert index_cache.cache_key(reference) != key
