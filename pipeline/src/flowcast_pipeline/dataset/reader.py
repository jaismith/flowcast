"""Minimal basin-block reader for the cube, plus a streaming benchmark.

A training data loader holds K basins' full series in memory, draws windows from them, then swaps in the
next K basins (rebuild plan §5.2). With one chunk per basin x whole period, loading a block is one ranged
read per basin and variable, which works the same from S3 or from a local NVMe copy.
"""

import resource
import time

import numpy as np
import zarr

HOURLY_PREFIXES = ("qobs_mm_h", "gauged_outflow_mm_h", "aorc_", "hrrr_an_", "mrms_")


def open_store(url: str) -> zarr.Group:
    if url.startswith("s3://"):
        return zarr.open_group(url, mode="r", storage_options={"anon": False})
    return zarr.open_group(url, mode="r")


def hourly_variables(group: zarr.Group) -> list[str]:
    return sorted(
        name for name, arr in group.arrays()
        if arr.metadata.dimension_names == ("basin", "time") and name.startswith(HOURLY_PREFIXES)
    )


def load_block(group: zarr.Group, basin_slice: slice, variables: list[str]) -> np.ndarray:
    """(basins, time, variables) float32 for a contiguous block of basins."""
    return np.stack([group[v][basin_slice] for v in variables], axis=-1)


def benchmark(url: str, k: int = 16, blocks: int = 3) -> dict:
    group = open_store(url)
    variables = hourly_variables(group)
    n = group["basin"].shape[0]
    times, nbytes = [], 0
    for b in range(min(blocks, -(-n // k))):
        t0 = time.time()
        block = load_block(group, slice(b * k, min((b + 1) * k, n)), variables)
        times.append(time.time() - t0)
        nbytes += block.nbytes
    return {
        "store": url,
        "variables": len(variables),
        "basins_per_block": k,
        "seconds_per_block": float(np.mean(times)),
        "mb_per_block": nbytes / len(times) / 1e6,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
    }
