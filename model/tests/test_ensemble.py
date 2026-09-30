import numpy as np
import pandas as pd

from flowcast_model.cli import main
from flowcast_model.ensemble import sample_cmal
from flowcast_model.units import to_cfs

ISSUES = pd.date_range("2021-01-01T12:00", periods=3, freq="D", tz="UTC")
LEADS = [1, 2, 6]


def _mixture(mu: float, members: list[int], b: float = 1e-4) -> pd.DataFrame:
    rows = [(i, l, m) for i in ISSUES for l in LEADS for m in members]
    frame = pd.DataFrame(rows, columns=["issue_time", "lead_h", "member"])
    frame["unit"] = "mm/h"
    for c in range(3):
        frame[f"pi{c}"] = [1.0, 0.0, 0.0][c]
        frame[f"mu{c}"] = mu
        frame[f"b{c}"] = b
        frame[f"tau{c}"] = 0.5
    return frame


def _write_run(root, site: str, mu: float) -> str:
    folder = root / f"site_id={site}"
    folder.mkdir(parents=True)
    _mixture(mu, [0, 1]).to_parquet(folder / "lstm.parquet", index=False)
    _mixture(mu, [-1]).to_parquet(folder / "lstm_perfect.parquet", index=False)
    return str(root)


def test_sample_cmal_follows_the_asymmetric_laplace_and_the_weights():
    rng = np.random.default_rng(0)
    n = 200_000
    one = sample_cmal(np.array([[1.0, 0.0]]), np.array([[2.0, 0.0]]), np.array([[0.5, 1.0]]), np.array([[0.3, 0.5]]), n, rng)
    assert abs((one < 2.0).mean() - 0.3) < 0.01
    two = sample_cmal(np.array([[0.25, 0.75]]), np.array([[-10.0, 10.0]]), np.array([[0.1, 0.1]]), np.array([[0.5, 0.5]]), n, rng)
    assert abs((two > 0).mean() - 0.75) < 0.01


def test_mixture_ensemble_pools_seeds_equally_in_harness_units(tmp_path, cube_path):
    site = "USGS-01000002"
    runs = [_write_run(tmp_path / "s42", site, 1.0), _write_run(tmp_path / "s43", site, 3.0)]
    _write_run(tmp_path / "s42", "USGS-01000001", 1.0)
    out = tmp_path / "pooled"
    main(["mixture-ensemble", "--mixtures", *runs, "--out", str(out), "--model", "lstm_pool", "--cube", str(cube_path), "--samples-per-member", "3"])

    assert sorted(p.name for p in out.iterdir()) == [f"site_id={site}"]
    op = pd.read_parquet(out / f"site_id={site}" / "lstm_pool.parquet")
    per_cell = op.groupby(["issue_time", "lead_h"])["member"].nunique()
    assert len(per_cell) == len(ISSUES) * len(LEADS) and (per_cell == 2 * 2 * 3).all()
    assert set(op["unit"]) == {"ft3/s"} and set(op["run_type"]) == {"operational"}
    assert ((op["valid_time"] - op["issue_time"]).dt.total_seconds() / 3600).isin(LEADS).all()
    low, high = to_cfs(np.array([1.0, 3.0]), "mm/h", 450.0)
    near_low = np.isclose(op["value"], low, rtol=1e-2)
    near_high = np.isclose(op["value"], high, rtol=1e-2)
    assert (near_low | near_high).all() and near_low.mean() == 0.5

    pf = pd.read_parquet(out / f"site_id={site}" / "lstm_pool_perfect.parquet")
    assert set(pf["model"]) == {"lstm_pool_perfect"} and set(pf["run_type"]) == {"perfect_forcing"}
    assert (pf.groupby(["issue_time", "lead_h"])["member"].nunique() == 2 * 3).all()


def test_mixture_ensemble_clips_at_zero_and_is_reproducible(tmp_path, cube_path):
    site = "USGS-01000003"
    runs = [_write_run(tmp_path / "a", site, 0.0)]
    frames = []
    for name, workers in (("x", "1"), ("y", "2")):
        main(["mixture-ensemble", "--mixtures", *runs, "--out", str(tmp_path / name), "--model", "m", "--cube", str(cube_path), "--workers", workers])
        frames.append(pd.read_parquet(tmp_path / name / f"site_id={site}" / "m.parquet"))
    assert (frames[0]["value"] >= 0).all() and (frames[0]["value"] == 0).any()
    pd.testing.assert_frame_equal(frames[0], frames[1])
