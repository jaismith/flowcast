"""Train -> interrupt -> resume -> hindcast -> score, on CPU with the synthetic cube."""

import json

import pandas as pd
import yaml

from flowcast_model.cli import main
from flowcast_model.trainer import latest_checkpoint


def tiny_config(tmp_path, cube_path, **kw) -> str:
    raw = {
        "flowcast": {
            "dataset": {"cube": [str(cube_path)], "optional_inputs": ["qobs_shift1"], "group_dropout": {"qobs_shift1": 0.5}, "block_basins": 2},
            "target": {"unit": "mm/h", "area_attribute": "area_km2"},
            "hindcast": {"issue_hours": [0, 12], "n_samples": 4, "epoch": "best"},
        },
        "experiment_name": "tiny",
        "train_start_date": "01/10/2018",
        "train_end_date": "30/09/2019",
        "validation_start_date": "01/10/2021",
        "validation_end_date": "30/09/2022",
        "test_start_date": "01/10/2021",
        "test_end_date": "30/09/2022",
        "model": "handoff_forecast_lstm",
        "seq_length": 72,
        "forecast_seq_length": 48,
        "predict_last_n": 48,
        "hidden_size": 8,
        "hindcast_hidden_size": 8,
        "forecast_hidden_size": 8,
        "state_handoff_network": {"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
        "dynamic_inputs": ["precip", "temp", "qobs_shift1"],
        "hindcast_inputs": [["precip", "temp"], ["qobs_shift1"]],
        "forecast_inputs": [["precip", "temp"]],
        "nan_handling_method": "input_replacing",
        "lagged_features": {"qobs": 1},
        "static_attributes": ["area_km2", "elev"],
        "target_variables": ["qobs"],
        "head": "cmal",
        "n_distributions": 2,
        "n_samples": 4,
        "negative_sample_handling": "clip",
        "loss": "cmalloss",
        "optimizer": "Adam",
        "learning_rate": 0.005,
        "batch_size": 32,
        "epochs": 2,
        "max_updates_per_epoch": 5,
        "num_workers": 0,
        "validate_every": 1,
        "validate_n_random_basins": 1,
        "metrics": ["NSE"],
        "save_validation_results": False,
        "device": "cpu",
        "verbose": 0,
        "log_tensorboard": False,
    }
    raw.update(kw)
    path = tmp_path / "tiny.yml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def test_train_resume_hindcast_score(tmp_path, cube_path):
    config = tiny_config(tmp_path, cube_path)
    run_dir = tmp_path / "run"
    main(["train", "--config", config, "--run-dir", str(run_dir)])
    assert latest_checkpoint(run_dir) == 2
    assert json.loads((run_dir / "checkpoint.json").read_text())["epoch"] == 2

    # simulate an interruption after epoch 2 of 3: the run resumes in place instead of starting over
    (run_dir / "model_epoch002.pt").rename(run_dir / "model_epoch002.pt.bak")
    main(["train", "--config", config, "--run-dir", str(run_dir), "--set", "epochs=3"])
    assert latest_checkpoint(run_dir) == 3
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events if e["event"] in ("start", "resume")] == ["start", "resume"]
    assert next(e for e in events if e["event"] == "resume")["epoch"] == 1
    assert (run_dir / "validation_metrics.csv").exists()

    # relaunching a finished run (e.g. to re-hindcast) keeps its config
    main(["train", "--config", config, "--run-dir", str(run_dir), "--set", "epochs=3"])
    assert (run_dir / "config.yml").exists() and latest_checkpoint(run_dir) == 3

    out = tmp_path / "hindcast"
    main(["hindcast", "--run-dir", str(run_dir), "--out", str(out)])
    parts = sorted(out.glob("site_id=*/*.parquet"))
    assert len(parts) == 3
    f = pd.read_parquet(parts[0])
    assert set(f["member"]) == {0, 1, 2, 3}
    assert f["lead_h"].max() == 48
    assert (pd.to_datetime(f["issue_time"]).dt.hour.isin([0, 12])).all()
    assert (f["value"] >= 0).all()

    score_dir = tmp_path / "score"
    main(["score", "--forecasts", str(out), "--cube", str(cube_path), "--out", str(score_dir), "--target", "qobs", "--nwm-attribute", "", "--n-boot", "20"])
    summary = pd.read_csv(score_dir / "summary.csv")
    assert {"tiny", "persistence", "climatology", "recession_persistence"} <= set(summary["model"])
    assert (score_dir / "summary.md").read_text().startswith("# Validation scoreboard")


def test_cmal_mixture_mean_matches_samples():
    import torch

    from flowcast_model.validation import mixture_mean

    torch.manual_seed(0)
    shape = (1, 1, 2)
    pred = {"mu": torch.tensor([[[0.0, 3.0]]]), "b": torch.tensor([[[0.5, 1.0]]]), "tau": torch.tensor([[[0.3, 0.6]]]), "pi": torch.tensor([[[0.4, 0.6]]])}
    n = 400_000
    comp = torch.multinomial(pred["pi"].reshape(-1), n, replacement=True)
    m, b, t = (pred[k].reshape(-1)[comp] for k in ("mu", "b", "tau"))
    u = torch.rand(n)
    x = torch.where(u < t, m + b * torch.log(u / t) / (1 - t), m - b * torch.log((1 - u) / (1 - t)) / t)
    assert abs(mixture_mean(pred, "cmal", 2).item() - x.mean().item()) < 0.02
    assert shape == pred["mu"].shape


def test_persistence_inputs_follow_last_observation_and_dropout(tmp_path, cube_path):
    import torch
    from neuralhydrology.datasetzoo import get_dataset

    from flowcast_model.config import prepare_run
    from flowcast_model.dataset import DatasetOptions, ZarrCubeDataset

    raw = yaml.safe_load(open(tiny_config(tmp_path, cube_path)))
    raw["dynamic_inputs"] = ["precip", "temp", "qobs_shift1", "qobs_persist"]
    raw["forecast_inputs"] = [["precip", "temp"], ["qobs_persist"]]
    cfg, options = prepare_run(raw, tmp_path / "run")
    cfg.train_dir = tmp_path / "run" / "train_data"
    cfg.train_dir.mkdir(parents=True)
    for p, expect_nan in ((0.0, False), (1.0, True)):
        ZarrCubeDataset.configure(DatasetOptions(optional_inputs=["qobs_shift1"], group_dropout={"qobs_shift1": p}, persist_inputs={"qobs_persist": "qobs_shift1"}))
        ds = get_dataset(cfg, is_train=True, period="train", scaler={})
        s = ds[100]
        persist = s["x_d_forecast"]["qobs_persist"]
        assert persist.shape == (48, 1)
        if expect_nan:
            assert torch.isnan(persist).all()
        else:
            assert torch.equal(persist, s["x_d_hindcast"]["qobs_shift1"][-1:].expand(48, 1))


def test_vectorized_cmal_sampler_matches_neuralhydrology_distribution():
    import torch

    from flowcast_model.hindcast import sample_mixture

    torch.manual_seed(0)
    B, S, K = 64, 30, 3
    pred = {"mu": torch.randn(B, S, K), "b": torch.rand(B, S, K) + 0.1, "tau": torch.rand(B, S, K) * 0.8 + 0.1, "pi": torch.softmax(torch.randn(B, S, K), -1)}
    pos = torch.tensor([4, 17, 29])
    x = sample_mixture(pred, "cmal", pos, K, 4000)
    assert x.shape == (B, 3, 4000)
    mu, b, tau, pi = (pred[k][:, pos] for k in ("mu", "b", "tau", "pi"))
    mean = (pi * (mu + b * (1 - 2 * tau) / (tau * (1 - tau)))).sum(-1)
    assert torch.allclose(x.mean(-1), mean, atol=0.25)


def test_residual_variant_adds_last_observation_to_forecast_location(tmp_path, cube_path):
    import torch
    from neuralhydrology.modelzoo import get_model

    from flowcast_model.config import prepare_run
    from flowcast_model.models import apply_variants

    cfg, options = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "run")
    torch.manual_seed(0)
    plain = get_model(cfg)
    torch.manual_seed(0)
    resid = apply_variants(get_model(cfg), {"residual_from": "qobs_shift1"})
    B, L, H = 3, 48, 24
    data = {"x_d_hindcast": {"precip": torch.randn(B, H, 1), "temp": torch.randn(B, H, 1), "qobs_shift1": torch.randn(B, H, 1)},
            "x_d_forecast": {"precip": torch.randn(B, L, 1), "temp": torch.randn(B, L, 1)}, "x_s": torch.randn(B, 2)}
    data["x_d_hindcast"]["qobs_shift1"][1, -1, 0] = float("nan")
    plain.eval(); resid.eval()
    a, b = plain(data)["mu"], resid(data)["mu"]
    last = data["x_d_hindcast"]["qobs_shift1"][:, -1, 0]
    assert torch.allclose(b[0, -L:] - a[0, -L:], last[0].expand(L, a.shape[-1]))
    assert torch.allclose(b[1], a[1])  # masked lagged flow: no offset
    assert torch.allclose(b[:, : H - L if H > L else 0], a[:, : H - L if H > L else 0])
    assert set(plain.state_dict()) == set(resid.state_dict())


def test_scale_floor_shifts_cmal_scale_only(tmp_path, cube_path):
    import torch
    from neuralhydrology.modelzoo import get_model

    from flowcast_model.config import prepare_run
    from flowcast_model.models import apply_variants

    cfg, _ = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "run")
    torch.manual_seed(0)
    plain = get_model(cfg)
    torch.manual_seed(0)
    floored = apply_variants(get_model(cfg), {"residual_from": "qobs_shift1", "min_scale": 1e-3})
    B, L, H = 3, 48, 24
    data = {"x_d_hindcast": {"precip": torch.randn(B, H, 1), "temp": torch.randn(B, H, 1), "qobs_shift1": torch.randn(B, H, 1)},
            "x_d_forecast": {"precip": torch.randn(B, L, 1), "temp": torch.randn(B, L, 1)}, "x_s": torch.randn(B, 2)}
    plain.eval(); floored.eval()
    a, b = plain(data), floored(data)
    assert "b" in a and torch.allclose(b["b"] - a["b"], torch.full_like(a["b"], 1e-3), atol=1e-7)
    assert b["b"].min() >= 1e-3 and torch.allclose(a["pi"], b["pi"]) and torch.allclose(a["tau"], b["tau"])
    assert set(plain.state_dict()) == set(floored.state_dict())


def test_amp_is_off_on_cpu_and_heads_stay_fp32(tmp_path, cube_path):
    import torch
    from neuralhydrology.modelzoo import get_model

    from flowcast_model.config import prepare_run
    from flowcast_model.models import apply_variants
    from flowcast_model.trainer import amp_dtype, heads_in_fp32

    assert amp_dtype("auto", torch.device("cpu")) is None and amp_dtype(None, torch.device("cuda")) is None
    cfg, _ = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "run")
    torch.manual_seed(0)
    plain = apply_variants(get_model(cfg), {"min_scale": 1e-3})
    torch.manual_seed(0)
    wrapped = heads_in_fp32(apply_variants(get_model(cfg), {"min_scale": 1e-3}))
    B, L, H = 2, 48, 24
    data = {"x_d_hindcast": {"precip": torch.randn(B, H, 1), "temp": torch.randn(B, H, 1), "qobs_shift1": torch.randn(B, H, 1)},
            "x_d_forecast": {"precip": torch.randn(B, L, 1), "temp": torch.randn(B, L, 1)}, "x_s": torch.randn(B, 2)}
    plain.eval(); wrapped.eval()
    a, b = plain(data), wrapped(data)
    assert all(torch.allclose(a[k], b[k]) for k in ("mu", "b", "tau", "pi")) and b["b"].dtype == torch.float32


def test_optimizer_guard_skips_non_finite_gradients():
    import torch

    from flowcast_model.trainer import FlowcastTrainer

    w = torch.nn.Parameter(torch.ones(3))
    stub = type("T", (), {})()
    stub.optimizer = torch.optim.SGD([w], lr=0.1)
    FlowcastTrainer._guard_optimizer(stub)
    w.grad = torch.tensor([1.0, float("inf"), 1.0])
    stub.optimizer.step()
    assert torch.equal(w.detach(), torch.ones(3)) and stub._skipped_steps == 1
    w.grad = torch.ones(3)
    stub.optimizer.step()
    assert torch.allclose(w.detach(), torch.full((3,), 0.9))


def test_bad_batches_are_logged_with_their_basins(tmp_path):
    import json

    import numpy as np
    import torch

    from flowcast_model.trainer import FlowcastTrainer

    stub = type("T", (), {})()
    stub.cfg = type("C", (), {"run_dir": tmp_path})()
    stub.loader = type("L", (), {})()
    stub.loader.dataset = type("D", (), {})()
    stub.loader.dataset.lookup_table = type("K", (), {"basins": ["A", "B", "C"]})()
    stub._current_epoch = 7
    dates = np.array([["2005-01-01T00", "2005-01-02T00"], ["2006-03-01T00", "2006-03-02T00"], ["2005-06-01T00", "2005-06-02T00"]], dtype="datetime64[h]")
    stub._current_batch = {"basin_index": torch.tensor([2, 0, 2]), "date": dates}
    FlowcastTrainer._report_bad_batch(stub, "non_finite_gradient")
    event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert event["event"] == "non_finite_gradient" and event["epoch"] == 7
    assert event["basins"] == {"A": 1, "C": 2}
    assert event["window_end"] == ["2005-01-02T00", "2006-03-02T00"]
