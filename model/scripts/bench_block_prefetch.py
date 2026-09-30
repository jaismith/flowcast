"""CPU data-loader benchmark for block prefetch (`flowcast.dataset.prefetch_basins`), off vs on.

The main process stands in for the GPU: it sleeps `--step-ms` per batch, and the time it spends waiting for the
next batch is GPU idle time. The loader is the trainer's (BasinBlockBatchSampler, persistent workers,
prefetch_factor 4). The synthetic cube matches the production basins' shape (30 hourly inputs, WY2001-2019
training period, seq_length 888 / forecast 168) with fewer basins. Blocks and updates per epoch are scaled
down together, so an epoch still spans about 2.3 blocks, as 1,200 updates of 512-batch blocks do.

    uv run python scripts/bench_block_prefetch.py --step-ms 85 --epochs 4 [--prefetch-basins 8]

Epoch 1 starts cold (no block was prefetched yet); the reported speedup compares the median of later epochs.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.utils.config import Config
from torch.utils.data import DataLoader

from flowcast_model.cube import write_cube
from flowcast_model.dataset import BasinBlockBatchSampler, DatasetOptions, ZarrCubeDataset

N_FEATURES = 30


def make_cube(root: Path, n_basins: int) -> tuple[Path, list[str]]:
    rng = np.random.default_rng(0)
    index = pd.date_range("2000-06-01", "2019-09-30T23:00", freq="h")
    basins = [f"0{1000001 + i}" for i in range(n_basins)]
    frames = {}
    for b in basins:
        data = {f"f{j:02d}": rng.normal(0, 1, len(index)).astype(np.float32) for j in range(N_FEATURES - 1)}
        data["qobs"] = rng.gamma(1.0, 0.1, len(index)).astype(np.float32)
        frames[b] = pd.DataFrame(data, index=index)
    static = pd.DataFrame({"area_km2": rng.uniform(50, 5000, n_basins), "elev": rng.uniform(100, 1500, n_basins)}, index=basins)
    path = root / "cube.zarr"
    write_cube(path, frames, static, {}, time_chunk=8760 * 4)
    return path, basins


def build(root: Path, cube: Path, basins: list[str], args, prefetch_basins: int):
    basin_file = root / "basins.txt"
    basin_file.write_text("\n".join(basins) + "\n")
    features = [f"f{j:02d}" for j in range(N_FEATURES - 1)] + ["qobs_shift1"]
    cfg = Config(
        {
            "experiment_name": "bench",
            "run_dir": str(root / "run"),
            "dataset": "flowcast_zarr",
            "data_dir": str(cube),
            "train_basin_file": str(basin_file),
            "validation_basin_file": str(basin_file),
            "test_basin_file": str(basin_file),
            "train_start_date": "01/10/2000",
            "train_end_date": "30/09/2019",
            "validation_start_date": "01/10/2000",
            "validation_end_date": "30/09/2019",
            "test_start_date": "01/10/2000",
            "test_end_date": "30/09/2019",
            "model": "handoff_forecast_lstm",
            "dynamic_inputs": features,
            "hindcast_inputs": [features[:8], features[8:-1], ["qobs_shift1"]],
            "forecast_inputs": [features[:7]],
            "nan_handling_method": "input_replacing",
            "lagged_features": {"qobs": 1},
            "target_variables": ["qobs"],
            "static_attributes": ["area_km2", "elev"],
            "seq_length": 888,
            "forecast_seq_length": 168,
            "predict_last_n": 168,
            "hidden_size": 8,
            "hindcast_hidden_size": 8,
            "forecast_hidden_size": 8,
            "state_handoff_network": {"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
            "head": "regression",
            "loss": "mse",
            "optimizer": "Adam",
            "learning_rate": 0.001,
            "batch_size": args.batch_size,
            "epochs": 1,
            "device": "cpu",
            "verbose": 0,
            "seed": 42,
        }
    )
    cfg.train_dir = root / f"train_{prefetch_basins}" / "train_data"
    cfg.train_dir.mkdir(parents=True, exist_ok=True)
    ZarrCubeDataset.configure(
        DatasetOptions(
            group_dropout={"qobs_shift1": 0.5, "f00": 0.3},
            block_basins=args.block_basins,
            chunk_samples=args.chunk_samples,
            prefetch_basins=prefetch_basins,
        )
    )
    return get_dataset(cfg, is_train=True, period="train", scaler={})


def decode_seconds(ds, basins: list[str], n: int = 4) -> float:
    t0 = time.perf_counter()
    for b in basins[:n]:
        ds._load_block(b)
    return (time.perf_counter() - t0) / n


def run(ds, args) -> dict:
    sampler = BasinBlockBatchSampler(ds.lookup_table, args.batch_size, args.block_basins, seed=42, chunk_samples=args.chunk_samples, prefetch=ds.prefetch_basins > 0, epoch_batches=args.updates)
    loader = DataLoader(ds, batch_sampler=sampler, num_workers=args.workers, collate_fn=ds.collate_fn, persistent_workers=True, prefetch_factor=4)
    torch.manual_seed(0)
    epochs, checksum = [], 0.0
    for epoch in range(1, args.epochs + 1):
        sampler.set_epoch(epoch)
        waits, long_waits = 0.0, []
        start = time.perf_counter()
        it = iter(loader)
        for i in range(args.updates):
            t0 = time.perf_counter()
            batch = next(it)
            wait = time.perf_counter() - t0
            waits += wait
            if wait > 0.3:
                long_waits.append([i, round(wait, 2)])
            checksum += float(torch.nan_to_num(batch["y"]).sum())
            time.sleep(args.step_ms / 1000)
        del it
        epochs.append({"epoch": epoch, "seconds": time.perf_counter() - start, "idle_seconds": waits, "waits_over_0.3s": long_waits})
    return {"epochs": epochs, "checksum": checksum}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # 128 basins in 16-basin blocks: consecutive blocks share about 12% of their basins, as 64 of 553 do
    p.add_argument("--basins", type=int, default=128)
    p.add_argument("--block-basins", type=int, default=16)
    p.add_argument("--chunk-samples", type=int, default=2048)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--updates", type=int, default=300, help="batches per epoch (max_updates_per_epoch)")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--step-ms", type=float, default=85.0, help="simulated GPU step per batch")
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--prefetch-basins", type=int, default=0, help="0 = the block size")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        t0 = time.perf_counter()
        cube, basins = make_cube(root, args.basins)
        print(f"cube written in {time.perf_counter() - t0:.0f} s", flush=True)
        results = {"args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}}
        for label, prefetch in (("off", 0), ("on", args.prefetch_basins or args.block_basins)):
            ds = build(root, cube, basins, args, prefetch)
            if label == "off":
                per_basin = decode_seconds(ds, basins)
                block_batches = args.block_basins * args.chunk_samples / args.batch_size
                results["decode_s_per_basin"] = per_basin
                results["block_decode_over_block_train"] = per_basin * args.block_basins / (block_batches * args.step_ms / 1000)
                print(f"decode {per_basin:.3f} s/basin; block decode / block train = {results['block_decode_over_block_train']:.2f}", flush=True)
            results[label] = run(ds, args)
            for e in results[label]["epochs"]:
                print(f"{label} epoch {e['epoch']}: {e['seconds']:.1f} s, idle {e['idle_seconds']:.1f} s, waits > 0.3 s at [batch, s]: {e['waits_over_0.3s']}", flush=True)
        assert results["off"]["checksum"] == results["on"]["checksum"], "batches differ"
        steady = lambda r: np.median([e["seconds"] for e in r["epochs"][1:]])  # noqa: E731
        results["steady_epoch_s"] = {"off": steady(results["off"]), "on": steady(results["on"])}
        results["speedup"] = results["steady_epoch_s"]["off"] / results["steady_epoch_s"]["on"]
        print(json.dumps({k: results[k] for k in ("steady_epoch_s", "speedup")}, indent=2))
        if args.out:
            args.out.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
