"""Block prefetch (`prefetch_basins`) changes when basins are decoded, never which samples a batch holds or their values."""

import numpy as np
import pandas as pd
import pytest
import torch
import xarray as xr
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.utils.config import Config
from torch.utils.data import DataLoader

from flowcast_model.dataset import BasinBlockBatchSampler, DatasetOptions, HintedBatch, ZarrCubeDataset

from .conftest import BASINS
from .test_dataset import base_cfg

EPOCH_BATCHES = 40
MODEL = dict(
    model="handoff_forecast_lstm",
    dynamic_inputs=["precip", "temp", "qobs_shift1", "gefs_precip"],
    hindcast_inputs=[["precip", "temp"], ["qobs_shift1"]],
    forecast_inputs=[["precip", "temp"], ["gefs_precip"]],
    nan_handling_method="masked_mean",
    seq_length=72,
    forecast_seq_length=24,
    predict_last_n=24,
    hindcast_hidden_size=8,
    forecast_hidden_size=8,
    state_handoff_network={"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
    batch_size=32,
    seed=7,
)


@pytest.fixture
def forecast_path(tmp_path):
    rng = np.random.default_rng(5)
    init = pd.date_range("2018-09-01", "2020-09-30", freq="12h")
    lead = np.arange(3, 49, 3, dtype=np.float32)
    values = rng.gamma(0.3, 1.0, (len(BASINS), len(init), 4, len(lead))).astype(np.float32)
    ds = xr.Dataset(
        {"gefs_precip": (("basin", "gefs_init", "gefs_member", "gefs_lead"), values)},
        coords={"basin": np.array(BASINS), "gefs_init": init.values, "gefs_lead": lead, "gefs_member": np.arange(4)},
    )
    path = tmp_path / "forecast.zarr"
    ds.to_zarr(path, consolidated=True)
    return path


def build(tmp_path, cube_path, forecast_path, name, prefetch_basins, chunk_samples=256):
    raw = {k: v for k, v in base_cfg(tmp_path, **MODEL).items() if v is not None}
    cfg = Config({**raw, "dataset": "flowcast_zarr", "data_dir": str(cube_path)})
    cfg.train_dir = tmp_path / name / "train_data"
    cfg.train_dir.mkdir(parents=True, exist_ok=True)
    # a 2-basin cache for 3 basins keeps evicting, so blocks are loaded (and prefetched) throughout
    ZarrCubeDataset.configure(
        DatasetOptions(
            cube=[str(cube_path), str(forecast_path)],
            optional_inputs=["qobs_shift1"],
            group_dropout={"qobs_shift1": 0.5},
            forecast_group_dropout={"gefs_precip": 0.3},
            block_basins=2,
            chunk_samples=chunk_samples,
            cache_basins=2,
            prefetch_basins=prefetch_basins,
        )
    )
    return get_dataset(cfg, is_train=True, period="train", scaler={})


def flatten(batch: dict, prefix: str = "") -> list[tuple[str, str, tuple, bytes]]:
    out = []
    for key in sorted(batch):
        value = batch[key]
        if isinstance(value, dict):
            out.extend(flatten(value, f"{prefix}{key}/"))
        else:
            arr = value.numpy() if isinstance(value, torch.Tensor) else np.asarray(value)
            out.append((prefix + key, str(arr.dtype), arr.shape, arr.tobytes()))
    return out


def train_batches(ds, workers: int, epochs: int) -> list:
    """Batches as the trainer draws them: persistent workers, set_epoch, max_updates_per_epoch."""
    sampler = BasinBlockBatchSampler(ds.lookup_table, 32, 2, seed=7, chunk_samples=ds.options.chunk_samples, prefetch=ds.prefetch_basins > 0, epoch_batches=EPOCH_BATCHES)
    loader = DataLoader(
        ds,
        batch_sampler=sampler,
        num_workers=workers,
        collate_fn=ds.collate_fn,
        persistent_workers=workers > 0,
        prefetch_factor=4 if workers > 0 else None,
    )
    torch.manual_seed(11)  # worker seeds derive from the main process's torch RNG
    np.random.seed(11)
    out = []
    for epoch in range(1, epochs + 1):
        sampler.set_epoch(epoch)
        for i, batch in enumerate(loader):
            if i >= EPOCH_BATCHES:
                break
            out.append(flatten(batch))
    return out


def assert_identical(a: list, b: list):
    assert len(a) == len(b)
    for n, (x, y) in enumerate(zip(a, b)):
        assert [k for k, *_ in x] == [k for k, *_ in y], n
        for (key, *vx), (_, *vy) in zip(x, y):
            assert vx == vy, f"batch {n}: {key} differs"


def test_prefetch_gives_bit_identical_batches_with_workers(tmp_path, cube_path, forecast_path):
    off = train_batches(build(tmp_path, cube_path, forecast_path, "off", 0), workers=3, epochs=3)
    on = train_batches(build(tmp_path, cube_path, forecast_path, "on", 2), workers=3, epochs=3)
    assert len(off) == 3 * EPOCH_BATCHES
    assert_identical(off, on)
    # the draws the check depends on did happen: group dropout masked some samples and not others
    masked = np.concatenate([np.isnan(np.frombuffer(v, np.float32).reshape(shape)).all(axis=(1, 2)) for batch in off for k, _, shape, v in batch if k == "x_d_hindcast/qobs_shift1"])
    assert 0.3 < masked.mean() < 0.7


def test_prefetch_gives_bit_identical_batches_in_process_and_uses_staged_blocks(tmp_path, cube_path, forecast_path):
    off = train_batches(build(tmp_path, cube_path, forecast_path, "off", 0), workers=0, epochs=2)
    ds = build(tmp_path, cube_path, forecast_path, "on", 3)
    on = train_batches(ds, workers=0, epochs=2)
    assert_identical(off, on)
    assert ds._prefetcher is not None and ds._prefetcher.hits > 0


@pytest.mark.parametrize("chunk_samples", [256, None])
def test_hinted_sampler_yields_the_same_indices_and_names_the_next_block(tmp_path, cube_path, forecast_path, chunk_samples):
    ds = build(tmp_path, cube_path, forecast_path, "s", 0, chunk_samples=chunk_samples)
    lookup = ds.lookup_table
    plain = BasinBlockBatchSampler(lookup, 32, 2, seed=7, chunk_samples=chunk_samples)
    hinted = BasinBlockBatchSampler(lookup, 32, 2, seed=7, chunk_samples=chunk_samples, prefetch=True, epoch_batches=EPOCH_BATCHES)
    offsets = lookup.offsets
    for epoch in (1, 2, 3):
        plain.set_epoch(epoch)
        hinted.set_epoch(epoch)
        a, b = list(plain), list(hinted)
        assert a == [list(x) for x in b]
        full = [x for x in b if isinstance(x, HintedBatch)]
        assert len(full) >= len(b) - 1
        for x in full:
            used = set((np.searchsorted(offsets, x, side="right") - 1).tolist())
            assert used <= set(x.hint.basins)
        blocks = {x.hint.block: x.hint for x in full}
        for key, hint in blocks.items():
            if hint.next_block in blocks:
                assert hint.next_basins == blocks[hint.next_block].basins
        # the block holding batch EPOCH_BATCHES names the first block of the next epoch
        cut = full[EPOCH_BATCHES - 1].hint
        assert cut.next_block == (hinted._pass + 1, 0)
        following = BasinBlockBatchSampler(lookup, 32, 2, seed=7, chunk_samples=chunk_samples, prefetch=True)
        following.set_epoch(epoch + 1)
        first = next(iter(following))
        carry_free = set((np.searchsorted(offsets, first, side="right") - 1).tolist())
        assert carry_free <= set(cut.next_basins)


def test_prefetch_is_off_by_default_and_for_evaluation(tmp_path, cube_path, forecast_path):
    assert DatasetOptions().prefetch_basins == 0
    ds = build(tmp_path, cube_path, forecast_path, "d", 0)
    assert ds.prefetch_basins == 0 and ds._blocks._load == ds._load_block
