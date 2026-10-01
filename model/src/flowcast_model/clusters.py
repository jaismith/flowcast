"""Basin clusters for fine-tuning (fine-tuning plan, section 2), from static attributes only so the scored years never
shape them: k-means on standardized attributes with bounds on the cluster sizes (Bradley, Bennett and Demiriz 2000,
"Constrained k-means clustering": every assignment step is a transportation problem whose capacities are the bounds;
its constraint matrix is totally unimodular, so the LP solution is integral). Plain k-means leaves the few western or
strongly regulated basins in clusters of 8-15, too small to fine-tune on.

The sweep has one run per cluster and, optionally, per-basin runs at named sites with the per-site recipe.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import linprog
from scipy.sparse import coo_matrix, vstack

from .cube import Cube

ATTRIBUTES = ("below_dam", "nid_dor_normal", "gauged_outflow_area_frac", "frac_snow", "log_area", "baseflow_index", "high_prec_freq", "lat", "lon")
SITE_OVERRIDES = {"batch_size": 512, "epochs": 10, "max_updates_per_epoch": 50}


def _assign(d2: np.ndarray, min_size: int, max_size: int) -> np.ndarray:
    """Cluster per point minimising the total squared distance, with every cluster's size in [min_size, max_size]."""
    n, k = d2.shape
    cols = np.arange(n * k)
    one_each = coo_matrix((np.ones(n * k), (np.repeat(np.arange(n), k), cols)), shape=(n, n * k))
    sizes = coo_matrix((np.ones(n * k), (np.tile(np.arange(k), n), cols)), shape=(k, n * k))
    res = linprog(
        d2.ravel(),
        A_ub=vstack([sizes, -sizes]).tocsr(),
        b_ub=np.concatenate([np.full(k, max_size), np.full(k, -min_size)]),
        A_eq=one_each.tocsr(),
        b_eq=np.ones(n),
        bounds=(0, 1),
        method="highs",
    )
    if not res.success:
        raise ValueError(f"no assignment with cluster sizes in [{min_size}, {max_size}]: {res.message}")
    return res.x.reshape(n, k).argmax(axis=1)


def _plus_plus(x: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    centers = [x[rng.integers(len(x))]]
    for _ in range(k - 1):
        d = ((x[:, None] - np.array(centers)[None]) ** 2).sum(-1).min(axis=1)
        centers.append(x[rng.choice(len(x), p=d / d.sum())])
    return np.array(centers)


def bounded_kmeans(x: np.ndarray, k: int, min_size: int, max_size: int, n_init: int = 20, seed: int = 0, max_iter: int = 100) -> np.ndarray:
    """Labels 0..k-1 of the lowest-inertia run of `n_init` k-means++ starts, numbered by descending cluster size."""
    if not k * min_size <= len(x) <= k * max_size:
        raise ValueError(f"{len(x)} points cannot form {k} clusters of {min_size}-{max_size}")
    rng = np.random.default_rng(seed)
    best, best_inertia = None, np.inf
    for _ in range(n_init):
        centers = _plus_plus(x, k, rng)
        labels = None
        for _ in range(max_iter):
            new = _assign(((x[:, None] - centers[None]) ** 2).sum(-1), min_size, max_size)
            if labels is not None and (new == labels).all():
                break
            labels = new
            centers = np.array([x[labels == j].mean(axis=0) for j in range(k)])
        inertia = float(((x - centers[labels]) ** 2).sum())
        if inertia < best_inertia - 1e-9:
            best, best_inertia = labels, inertia
    order = np.argsort(-np.bincount(best, minlength=k), kind="stable")
    return np.argsort(order)[best]


def cluster_basins(cube: Cube, basins: list[str], k: int = 16, min_size: int = 20, max_size: int = 60, seed: int = 0, attributes=ATTRIBUTES) -> pd.DataFrame:
    """One row per basin: its cluster (`c00` is the largest) and the attributes it was clustered on."""
    attrs = cube.load_static(basins, list(attributes))
    if attrs.isna().any().any():
        raise ValueError(f"missing attributes: {attrs.columns[attrs.isna().any()].tolist()}")
    std = attrs.std(ddof=0).replace(0, 1)
    labels = bounded_kmeans(((attrs - attrs.mean()) / std).to_numpy(), k, min_size, max_size, seed=seed)
    return attrs.assign(cluster=[f"c{j:02d}" for j in labels])[["cluster", *attributes]]


def sweep(assignment: pd.DataFrame, config: str, name: str, sites: dict[str, str] | None = None, dataset: str = "s3://flowcast-dataset-257129854363/v1.3/full/trainval.zarr") -> dict:
    """Sweep spec: per-basin site runs first (short; they check the job end to end), then one run per cluster."""
    runs = {f"site_{label}": {"flowcast.basins": [basin], **SITE_OVERRIDES} for label, basin in (sites or {}).items()}
    for cluster, g in assignment.groupby("cluster"):
        runs[cluster] = {"flowcast.basins": sorted(g.index)}
    return {"name": name, "config": config, "datasets": [dataset], "instance_type": "auto", "max_hours": 6, "runs": runs}


def write(assignment: pd.DataFrame, spec: dict, csv_path: str | Path, sweep_path: str | Path, header: str = "") -> None:
    assignment.rename_axis("basin").reset_index().to_csv(csv_path, index=False, float_format="%.6g")

    class Flow(yaml.SafeDumper):
        pass

    Flow.add_representer(list, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))
    Path(sweep_path).write_text(header + yaml.dump(spec, Dumper=Flow, sort_keys=False, width=120))
