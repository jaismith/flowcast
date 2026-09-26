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
