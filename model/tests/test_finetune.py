import numpy as np
import pandas as pd
import yaml
from neuralhydrology.training.earlystopper import EarlyStopper

from flowcast_model.clusters import SITE_OVERRIDES, bounded_kmeans, sweep, write
from flowcast_model.trainer import replay_early_stopping


def test_bounded_kmeans_respects_size_bounds():
    rng = np.random.default_rng(1)
    # one large blob and two small, far-away ones: plain k-means would give clusters of 50, 6 and 4
    x = np.vstack([rng.normal(0, 1, (50, 2)), rng.normal(10, 0.1, (6, 2)), rng.normal(-10, 0.1, (4, 2))])
    labels = bounded_kmeans(x, 3, min_size=15, max_size=30, n_init=3)
    sizes = np.bincount(labels, minlength=3)
    assert sizes.min() >= 15 and sizes.max() <= 30 and sizes.sum() == 60
    assert list(sizes) == sorted(sizes, reverse=True)
    # the small blobs stay whole
    assert len(set(labels[50:56])) == 1 and len(set(labels[56:])) == 1


def test_bounded_kmeans_is_deterministic():
    x = np.random.default_rng(2).normal(size=(80, 3))
    assert (bounded_kmeans(x, 4, 10, 30, n_init=4, seed=5) == bounded_kmeans(x, 4, 10, 30, n_init=4, seed=5)).all()


def test_sweep_lists_sites_then_clusters(tmp_path):
    assignment = pd.DataFrame({"cluster": ["c00", "c00", "c01"], "lat": [40.0, 41.0, 42.0]}, index=pd.Index(["02", "01", "03"]))
    spec = sweep(assignment, "configs/final_snodas_finetune.yml", "ft", {"callicoon": "01"})
    assert list(spec["runs"]) == ["site_callicoon", "c00", "c01"]
    assert spec["runs"]["site_callicoon"] == {"flowcast.basins": ["01"], **SITE_OVERRIDES}
    assert spec["runs"]["c00"] == {"flowcast.basins": ["01", "02"]}
    write(assignment, spec, tmp_path / "a.csv", tmp_path / "s.yml", "# header\n")
    assert yaml.safe_load((tmp_path / "s.yml").read_text()) == spec
    assert pd.read_csv(tmp_path / "a.csv", dtype={"basin": str})["basin"].tolist() == ["02", "01", "03"]


def _metrics(tmp_path, losses):
    pd.DataFrame({"epoch": range(1, len(losses) + 1), "avg_total_loss": losses}).to_csv(tmp_path / "validation_metrics.csv", index=False)


def test_replay_early_stopping(tmp_path):
    _metrics(tmp_path, [1.0, 0.9, 0.95, 0.91, 0.8])
    # patience 2: epochs 3 and 4 don't improve on epoch 2, so training stopped at 4 (epoch 5 is never reached)
    assert replay_early_stopping(EarlyStopper(2, 0.0001), tmp_path, 5, 0) == 4
    # resumed after epoch 3: no stop yet, and the stopper carries one epoch without improvement
    stopper = EarlyStopper(2, 0.0001)
    assert replay_early_stopping(stopper, tmp_path, 3, 0) is None
    assert stopper.check_early_stopping(0.91)
    assert replay_early_stopping(EarlyStopper(2, 0.0001), tmp_path / "missing", 5, 0) is None
