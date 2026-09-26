"""The streaming dataset on a cube laid out like the training-dataset v1 builder writes it
(pipeline/src/flowcast_pipeline/dataset/cube.py on cursor/training-dataset-v1-0e20)."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from neuralhydrology.datasetzoo import get_dataset
from neuralhydrology.utils.config import Config

from flowcast_model.cube import Cube
from flowcast_model.dataset import DatasetOptions, ZarrCubeDataset

BASINS = ["01000001", "01000002"]
EPOCH = pd.Timestamp("2000-01-01")


def hours(t: pd.DatetimeIndex) -> np.ndarray:
    return ((t - EPOCH) / pd.Timedelta(hours=1)).to_numpy().astype(np.int64)


@pytest.fixture
def worker_cube(tmp_path):
    rng = np.random.default_rng(1)
    time = pd.date_range("2019-01-01", "2020-12-31T23:00", freq="h")
    nb, nt = len(BASINS), len(time)
    hrrr_init = pd.date_range("2019-01-01", "2020-12-31", freq="6h")
    gefs_init = pd.date_range("2019-01-01", "2020-12-31", freq="12h")
    hrrr_lead = np.arange(1, 49, dtype=np.float32)
    gefs_lead = np.arange(3, 241, 3, dtype=np.float32)
    ds = xr.Dataset(
        {
            "qobs_mm_h": (("basin", "time"), rng.gamma(1.0, 0.1, (nb, nt)).astype(np.float32)),
            "aorc_precip_mm_h": (("basin", "time"), rng.gamma(0.3, 1.0, (nb, nt)).astype(np.float32)),
            "aorc_band_temp_2m_c": (("basin", "band", "time"), rng.normal(5, 8, (nb, 4, nt)).astype(np.float32)),
            # forecast value encodes (init index, lead) so the test can check which init and lead were used
            "hrrr_fc_precip_mm_h": (("basin", "hrrr_init", "hrrr_lead"), (np.arange(len(hrrr_init))[None, :, None] * 1000 + hrrr_lead[None, None, :]).repeat(nb, 0).astype(np.float32)),
            "gefs_precip_mm_h": (("basin", "gefs_init", "gefs_member", "gefs_lead"), (np.arange(len(gefs_init))[None, :, None, None] * 1000 + gefs_lead[None, None, None, :] + 0.1 * np.arange(5)[None, None, :, None]).repeat(nb, 0).astype(np.float32)),
            "area_km2": (("basin",), np.array([100.0, 900.0], dtype=np.float32)),
            "elev_band_mean_m": (("basin", "band"), rng.normal(500, 100, (nb, 4)).astype(np.float32)),
            "static_all": (("basin", "attribute"), rng.normal(0, 1, (nb, 3)).astype(np.float32)),
        },
        coords={
            "basin": np.array(BASINS, dtype="<U8"),
            "time": ("time", hours(time), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "band": np.arange(4, dtype=np.int8),
            "attribute": np.array(["frac_forest", "slope_mean", "baseflow_index"]),
            "hrrr_init": ("hrrr_init", hours(hrrr_init), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "hrrr_lead": hrrr_lead,
            "gefs_init": ("gefs_init", hours(gefs_init), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "gefs_lead": gefs_lead,
            "gefs_member": np.arange(5, dtype=np.int8),
        },
    )
    path = tmp_path / "trainval.zarr"
    ds.to_zarr(path, zarr_format=3, consolidated=False)
    return path, hrrr_init, gefs_init


def test_cube_indexes_worker_layout(worker_cube):
    path, _, _ = worker_cube
    cube = Cube([path])
    assert cube.kind("aorc_band_temp_2m_c_band3") == "dynamic"
    assert cube.kind("elev_band_mean_m_band0") == "static"
    assert cube.kind("slope_mean") == "static"
    assert cube.kind("gefs_precip_mm_h") == "forecast" and cube.forecast_product("gefs_precip_mm_h") == "gefs_init"
    df = cube.load_dynamic("01000002", ["aorc_band_temp_2m_c_band3"], pd.Timestamp("2019-02-01"), pd.Timestamp("2019-02-02"))
    assert len(df) == 25 and df.notna().all().all()
    products = cube.load_forecast("01000001", ["hrrr_fc_precip_mm_h", "gefs_precip_mm_h"], pd.Timestamp("2019-03-01"), pd.Timestamp("2019-03-02"))
    issues, leads, values, names = products["gefs_init"]
    assert values.shape == (3, 80, 5, 1) and names == ["gefs_precip_mm_h"]


def test_forecast_inputs_come_from_latest_init(tmp_path, worker_cube):
    path, hrrr_init, gefs_init = worker_cube
    basin_file = tmp_path / "basins.txt"
    basin_file.write_text("\n".join(BASINS))
    cfg = Config(
        {
            "experiment_name": "w",
            "run_dir": str(tmp_path / "run"),
            "data_dir": str(path),
            "dataset": "flowcast_zarr",
            "train_basin_file": str(basin_file),
            "validation_basin_file": str(basin_file),
            "test_basin_file": str(basin_file),
            "train_start_date": "01/03/2019",
            "train_end_date": "30/06/2020",
            "validation_start_date": "01/07/2020",
            "validation_end_date": "30/09/2020",
            "test_start_date": "01/07/2020",
            "test_end_date": "30/09/2020",
            "model": "handoff_forecast_lstm",
            "dynamic_inputs": ["aorc_precip_mm_h", "aorc_band_temp_2m_c_band3", "hrrr_fc_precip_mm_h", "gefs_precip_mm_h"],
            "hindcast_inputs": ["aorc_precip_mm_h", "aorc_band_temp_2m_c_band3"],
            "forecast_inputs": ["hrrr_fc_precip_mm_h", "gefs_precip_mm_h"],
            "nan_handling_method": "input_replacing",
            "static_attributes": ["area_km2", "slope_mean", "elev_band_mean_m_band1"],
            "target_variables": ["qobs_mm_h"],
            "seq_length": 96,
            "forecast_seq_length": 72,
            "predict_last_n": 72,
            "hidden_size": 8,
            "hindcast_hidden_size": 8,
            "forecast_hidden_size": 8,
            "state_handoff_network": {"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
            "loss": "mse",
            "head": "regression",
            "optimizer": "Adam",
            "learning_rate": 0.001,
            "batch_size": 8,
            "epochs": 1,
            "device": "cpu",
            "verbose": 0,
        }
    )
    cfg.train_dir = tmp_path / "train_data"
    cfg.train_dir.mkdir()
    ZarrCubeDataset.configure(DatasetOptions(forecast_latency_h={"hrrr_init": 2, "gefs_init": 5}))
    train = get_dataset(cfg, is_train=True, period="train", scaler={})
    ds = get_dataset(cfg, is_train=False, period="validation", basin=BASINS[0], scaler=train.scaler)
    ds.forecast_member = 3
    scaler = train.scaler
    for item in (0, 17, 500):
        basin, (idx,) = ds.lookup_table[item]
        issue = pd.Timestamp(ds._blocks[basin]["dates"][ds.frequencies[0]][idx - 72])
        sample = ds[item]
        for name, inits, latency, leads in (("hrrr_fc_precip_mm_h", hrrr_init, 2, np.arange(1, 49)), ("gefs_precip_mm_h", gefs_init, 5, np.arange(3, 241, 3))):
            raw = sample["x_d_forecast"][name][:, 0].numpy() * float(scaler["xarray_feature_scale"][name]) + float(scaler["xarray_feature_center"][name])
            pos = inits.searchsorted(issue - pd.Timedelta(hours=latency), side="right") - 1
            offset = (issue - inits[pos]) / pd.Timedelta(hours=1)
            for h in (1, 2, 30, 72):
                want_lead = leads[np.searchsorted(leads, offset + h)] if offset + h <= leads[-1] else None
                if want_lead is None or want_lead - (offset + h) >= (leads[1] - leads[0]):
                    assert np.isnan(raw[h - 1])
                else:
                    expected = pos * 1000 + want_lead + (0.3 if name.startswith("gefs") else 0.0)
                    assert raw[h - 1] == pytest.approx(expected, rel=1e-4), (name, h, issue)
    assert "slope_mean" in train.scaler["attribute_means"].index
