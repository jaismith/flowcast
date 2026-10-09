"""Train -> interrupt -> resume -> hindcast -> score, on CPU with the synthetic cube."""

import json

import numpy as np
import pandas as pd
import torch
import yaml
from neuralhydrology.training import get_loss_obj

from flowcast_model.cli import main
from flowcast_model import trainer as trainer_module
from flowcast_model.config import prepare_run
from flowcast_model.dataset import ZarrCubeDataset
from flowcast_model.models import weight_cmal_loss
from flowcast_model.trainer import AMP_FALLBACK_FILE, FlowcastTrainer, latest_checkpoint


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


def test_hindcast_saves_the_cmal_mixture(tmp_path, cube_path):
    flowcast = {
        "dataset": {"cube": [str(cube_path)], "optional_inputs": ["qobs_shift1"], "group_dropout": {"qobs_shift1": 0.5}, "block_basins": 2},
        "target": {"unit": "mm/h", "area_attribute": "area_km2"},
        "hindcast": {"issue_hours": [0, 12], "n_samples": 4, "epoch": "best", "save_mixture": True},
    }
    config = tiny_config(tmp_path, cube_path, flowcast=flowcast)
    run_dir = tmp_path / "run"
    main(["train", "--config", config, "--run-dir", str(run_dir)])
    out = tmp_path / "hindcast"
    main(["hindcast", "--run-dir", str(run_dir), "--out", str(out)])
    forecasts = sorted(out.glob("site_id=*/*.parquet"))
    mixtures = sorted((tmp_path / "hindcast_mixture").glob("site_id=*/*.parquet"))
    assert len(forecasts) == len(mixtures) == 3
    f, m = pd.read_parquet(forecasts[0]), pd.read_parquet(mixtures[0])
    assert len(m) == f.groupby(["issue_time", "lead_h"]).ngroups  # one row per issue and lead (one member)
    assert set(m["member"]) == {-1} and (m["unit"] == "mm/h").all()
    assert np.allclose(m[["pi0", "pi1"]].sum(axis=1), 1.0, atol=1e-5)
    assert (m[["b0", "b1"]] > 0).all().all() and m[["tau0", "tau1"]].stack().between(0, 1).all()


def test_hindcast_all_leads_writes_every_hour_and_its_daily_maxima(tmp_path, cube_path):
    flowcast = {
        "dataset": {"cube": [str(cube_path)], "optional_inputs": ["qobs_shift1"], "group_dropout": {"qobs_shift1": 0.5}, "block_basins": 2},
        "target": {"unit": "mm/h", "area_attribute": "area_km2", "daily_max": {"timezone": "America/New_York"}},
        "hindcast": {"issue_hours": [0, 12], "n_samples": 4, "epoch": "best", "save_mixture": True, "coherent_samples": True, "all_leads": True},
    }
    config = tiny_config(tmp_path, cube_path, flowcast=flowcast)
    run_dir = tmp_path / "run"
    main(["train", "--config", config, "--run-dir", str(run_dir)])
    out = tmp_path / "hindcast"
    main(["hindcast", "--run-dir", str(run_dir), "--out", str(out)])
    f = pd.read_parquet(sorted(out.glob("site_id=*/*.parquet"))[0])
    m = pd.read_parquet(sorted((tmp_path / "hindcast_mixture").glob("site_id=*/*.parquet"))[0])
    hourly, daily = f[f["variable"] == "discharge"], f[f["variable"] == "discharge_daily_max"]
    assert sorted(hourly["lead_h"].unique()) == sorted(m["lead_h"].unique()) == list(range(1, 49))
    assert len(daily)

    # the stored daily maxima are each sample path's maximum over the stored hours of that local date
    local = (hourly["valid_time"] - pd.Timedelta(hours=1)).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    derived = hourly.assign(day=local.dt.tz_localize("UTC")).groupby(["issue_time", "day", "member"])["value"].max()
    stored = daily.set_index(["issue_time", "valid_time", "member"])["value"]
    assert np.allclose(derived.reindex(stored.index.rename(derived.index.names)).to_numpy(), stored.to_numpy())


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


def test_fp16_loss_scaler_leaves_the_feature_scaler_alone(tmp_path, cube_path, monkeypatch):
    import torch

    from flowcast_model import trainer as trainer_module
    from flowcast_model.config import prepare_run

    monkeypatch.setattr(trainer_module, "amp_dtype", lambda setting, device: torch.float16)
    cfg, _ = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "run")
    t = trainer_module.FlowcastTrainer(cfg, train_options={"amp": "fp16"})
    assert isinstance(t._grad_scaler, torch.amp.GradScaler) and t._scaler == {}
    t.initialize_training()
    assert "xarray_feature_center" in t.loader.dataset.scaler


def test_init_from_starts_from_another_runs_scaler_and_weights(tmp_path, cube_path):
    import torch

    from flowcast_model import trainer as trainer_module
    from flowcast_model.config import prepare_run

    base = tmp_path / "base"
    main(["train", "--config", tiny_config(tmp_path, cube_path), "--run-dir", str(base)])
    cfg, _ = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "tuned")
    t = trainer_module.FlowcastTrainer(cfg, train_options={"init_from": str(base), "init_epoch": 2})
    t.initialize_training()
    ref = torch.load(base / "model_epoch002.pt")
    assert all(torch.equal(v, ref[k]) for k, v in t.model.state_dict().items())
    scaler = "train_data/train_data_scaler.yml"
    assert (tmp_path / "tuned" / scaler).read_text() == (base / scaler).read_text()
    assert trainer_module.fetch_init(str(base), tmp_path / "best", "best").name.startswith("model_epoch")


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


def test_flow_weight_loss_is_the_stock_loss_with_equal_weights(tmp_path, cube_path):
    cfg, _ = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path))), tmp_path / "run")
    stock, weighted = get_loss_obj(cfg), weight_cmal_loss(get_loss_obj(cfg))
    torch.manual_seed(0)
    B, T, K = 4, 48, 2
    pred = {"mu": torch.randn(B, T, K), "b": torch.rand(B, T, K) + 0.1, "tau": torch.rand(B, T, K) * 0.8 + 0.1, "pi": torch.softmax(torch.randn(B, T, K), -1)}
    y = torch.randn(B, T, 1)
    y[3, 5, 0] = float("nan")
    base = stock(pred, {"y": y})[0]
    assert torch.allclose(weighted(pred, {"y": y})[0], base)
    assert torch.allclose(weighted(pred, {"y": y, "flow_weight": torch.full_like(y, 2.5)})[0], base)

    w = torch.ones_like(y)
    w[0, :10] = 5.0
    error = y[:3] - pred["mu"][:3]
    t, b = pred["tau"][:3], pred["b"][:3]
    ll = torch.log(t) + torch.log(1 - t) - torch.log(b) - torch.max(t * error, (t - 1) * error) / b
    step = torch.logsumexp(torch.log(pred["pi"][:3] + 1e-8) + ll, dim=2)
    expected = -(step * w[:3, :, 0] / w[:3, :, 0].mean()).sum(1).mean()
    assert torch.allclose(weighted(pred, {"y": y, "flow_weight": w})[0], expected)


def test_training_with_flow_weight(tmp_path, cube_path):
    flowcast = {
        "dataset": {"cube": [str(cube_path)], "optional_inputs": ["qobs_shift1"], "block_basins": 2,
                    "flow_weight": {"high_quantile": 0.9, "high": 3.0, "rise_quantile": 0.5, "rise": 2.0}},
        "target": {"unit": "mm/h", "area_attribute": "area_km2"},
        "hindcast": {"issue_hours": [0, 12], "n_samples": 4, "epoch": "best"},
    }
    run_dir = tmp_path / "run"
    main(["train", "--config", tiny_config(tmp_path, cube_path, flowcast=flowcast, epochs=1), "--run-dir", str(run_dir)])
    assert latest_checkpoint(run_dir) == 1
    assert np.isfinite(pd.read_csv(run_dir / "validation_metrics.csv")["avg_total_loss"]).all()


def test_amp_fallback_redoes_a_broken_epoch_in_fp32_and_stays_there(tmp_path, cube_path, monkeypatch):
    cfg, options = prepare_run(yaml.safe_load(open(tiny_config(tmp_path, cube_path, epochs=3))), tmp_path / "run")
    ZarrCubeDataset.configure(options.dataset)
    trainer = FlowcastTrainer(cfg, model_options=options.model, train_options=options.train)
    trainer.initialize_training()
    real, calls = FlowcastTrainer._run_epoch, []

    def run_epoch(self, epoch):
        calls.append((epoch, self._amp))
        if self._amp is None:
            return real(self, epoch)
        if epoch == 2:
            self._epoch_nan = 3  # 3 of 5 steps skipped
        return None

    monkeypatch.setattr(FlowcastTrainer, "_run_epoch", run_epoch)
    trainer._amp = torch.bfloat16
    trainer.train_and_validate()
    assert calls == [(1, torch.bfloat16), (2, torch.bfloat16), (2, None), (3, None)]
    marker = json.loads((tmp_path / "run" / AMP_FALLBACK_FILE).read_text())
    assert marker["epoch"] == 2 and "3 of 5" in marker["reason"]
    events = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    assert [e["epoch"] for e in events if e["event"] == "amp_fallback"] == [2]
    assert latest_checkpoint(tmp_path / "run") == 3

    monkeypatch.setattr(trainer_module, "amp_dtype", lambda setting, device: torch.bfloat16)
    resumed = FlowcastTrainer(cfg, model_options=options.model, train_options={"amp": "bf16"})
    assert resumed._amp is None
