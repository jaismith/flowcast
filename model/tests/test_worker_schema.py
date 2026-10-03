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
    gefs_init = pd.date_range("2020-01-01", "2020-12-31", freq="12h")
    rf_init = pd.date_range("2019-01-01", "2019-10-31", freq="D")
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
            "gefs_band_temp_2m_c": (("basin", "gefs_init", "gefs_member", "gefs_lead", "band"), (np.arange(len(gefs_init))[None, :, None, None, None] * 1000 + gefs_lead[None, None, None, :, None] + 0.1 * np.arange(4)[None, None, None, None, :] + 0.0 * np.arange(5)[None, None, :, None, None]).repeat(nb, 0).astype(np.float32)),
            # travel-time zones: basin 0 doesn't reach zone 2
            "aorc_tz3_precip_mm_h": (("basin", "tz_coarse", "time"), np.where(np.arange(3)[None, :, None] == 2, np.array([np.nan, 1.0])[:, None, None], 0.5) * np.ones((nb, 3, nt), dtype=np.float32)),
            "gefs_tz3_precip_mm_h": (("basin", "gefs_init", "gefs_member", "gefs_lead", "tz_coarse"), np.where(np.arange(3) == 2, np.array([np.nan, 2.0])[:, None, None, None, None], 0.25) * np.ones((nb, len(gefs_init), 5, len(gefs_lead), 3), dtype=np.float32)),
            "tz3_area_frac": (("basin", "tz_coarse"), np.array([[0.4, 0.6, 0.0], [0.2, 0.3, 0.5]], dtype=np.float32)),
            # reforecast: inits end before the operational ones start; value encodes 10,000,000 + init index * 1000 + lead
            "gefs_rf_precip_mm_h": (("basin", "gefs_rf_init", "gefs_rf_member", "gefs_rf_lead"), (10_000_000 + np.arange(len(rf_init))[None, :, None, None] * 1000 + gefs_lead[None, None, None, :] + 0.0 * np.arange(3)[None, None, :, None]).repeat(nb, 0).astype(np.float32)),
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
            "gefs_rf_init": ("gefs_rf_init", hours(rf_init), {"units": "hours since 2000-01-01 00:00:00", "calendar": "proleptic_gregorian"}),
            "gefs_rf_lead": gefs_lead,
            "gefs_rf_member": np.arange(3, dtype=np.int8),
        },
    )
    path = tmp_path / "trainval.zarr"
    ds.to_zarr(path, zarr_format=3, consolidated=False)
    return path, hrrr_init, gefs_init, rf_init


def test_cube_indexes_worker_layout(worker_cube):
    path, _, gefs_init, _ = worker_cube
    cube = Cube([path])
    assert cube.kind("aorc_band_temp_2m_c_band3") == "dynamic"
    assert cube.kind("elev_band_mean_m_band0") == "static"
    assert cube.kind("slope_mean") == "static"
    assert cube.kind("gefs_precip_mm_h") == "forecast" and cube.forecast_product("gefs_precip_mm_h") == "gefs_init"
    df = cube.load_dynamic("01000002", ["aorc_band_temp_2m_c_band3"], pd.Timestamp("2019-02-01"), pd.Timestamp("2019-02-02"))
    assert len(df) == 25 and df.notna().all().all()
    products = cube.load_forecast("01000001", ["hrrr_fc_precip_mm_h", "gefs_precip_mm_h"], pd.Timestamp("2020-03-01"), pd.Timestamp("2020-03-02"))
    issues, leads, values, names = products["gefs_init"]
    assert values.shape == (3, 80, 5, 1) and names == ["gefs_precip_mm_h"]
    assert cube.kind("gefs_band_temp_2m_c_band2") == "forecast" and not cube.has("gefs_band_temp_2m_c")
    issues, leads, values, names = cube.load_forecast("01000001", ["gefs_band_temp_2m_c_band0", "gefs_band_temp_2m_c_band2"], pd.Timestamp("2020-03-01"), pd.Timestamp("2020-03-02"))["gefs_init"]
    first = gefs_init.get_loc(issues[0])
    np.testing.assert_allclose(values[0, 1, 3], [first * 1000 + 6.0, first * 1000 + 6.2], rtol=1e-6)


def test_threaded_reads_match_serial_reads(worker_cube):
    path, _, _, _ = worker_cube
    serial, threaded = Cube([path]), Cube([path], read_threads=4)
    features = ["qobs_mm_h", "aorc_precip_mm_h", "aorc_band_temp_2m_c_band1", "aorc_band_temp_2m_c_band3", "aorc_tz3_precip_mm_h_tz_coarse2"]
    start, end = pd.Timestamp("2019-03-01"), pd.Timestamp("2020-02-01")
    pd.testing.assert_frame_equal(serial.load_dynamic(BASINS[0], features, start, end), threaded.load_dynamic(BASINS[0], features, start, end))
    forecast = ["gefs_precip_mm_h", "gefs_band_temp_2m_c_band0", "gefs_band_temp_2m_c_band3", "hrrr_fc_precip_mm_h"]
    normalize = lambda f, leads, x: (x - 3.0) / 7.0  # noqa: E731
    a = serial.load_forecast(BASINS[1], forecast, start, end)
    b = threaded.load_forecast(BASINS[1], forecast, start, end, transform=normalize, dtype=np.float16)
    assert a.keys() == b.keys()
    for product in a:
        assert a[product][3] == b[product][3] and b[product][2].dtype == np.float16
        np.testing.assert_array_equal(((a[product][2] - 3.0) / 7.0).astype(np.float16), b[product][2])


def test_forecast_inputs_come_from_latest_init(tmp_path, worker_cube):
    path, hrrr_init, gefs_init, rf_init = worker_cube
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


def test_reforecast_fills_the_operational_input_before_it_starts(tmp_path, worker_cube):
    path, _, gefs_init, rf_init = worker_cube
    basin_file = tmp_path / "basins.txt"
    basin_file.write_text("\n".join(BASINS))
    base = dict(
        experiment_name="w", run_dir=str(tmp_path / "run"), data_dir=str(path), dataset="flowcast_zarr",
        train_basin_file=str(basin_file), validation_basin_file=str(basin_file), test_basin_file=str(basin_file),
        train_start_date="01/03/2019", train_end_date="31/08/2019", validation_start_date="01/09/2019", validation_end_date="30/06/2020",
        test_start_date="01/09/2019", test_end_date="30/06/2020", model="handoff_forecast_lstm",
        dynamic_inputs=["aorc_precip_mm_h", "gefs_precip_mm_h"], hindcast_inputs=["aorc_precip_mm_h"], forecast_inputs=["gefs_precip_mm_h"],
        nan_handling_method="input_replacing", static_attributes=["area_km2"], target_variables=["qobs_mm_h"],
        seq_length=96, forecast_seq_length=72, predict_last_n=72, hidden_size=8, hindcast_hidden_size=8, forecast_hidden_size=8,
        state_handoff_network={"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
        loss="mse", head="regression", optimizer="Adam", learning_rate=0.001, batch_size=8, epochs=1, device="cpu", verbose=0,
    )
    cfg = Config(base)
    cfg.train_dir = tmp_path / "train_data"
    cfg.train_dir.mkdir()
    ZarrCubeDataset.configure(DatasetOptions(forecast_aliases={"gefs_rf_precip_mm_h": "gefs_precip_mm_h"}))
    train = get_dataset(cfg, is_train=True, period="train", scaler={})
    center, scale = float(train.scaler["xarray_feature_center"]["gefs_precip_mm_h"]), float(train.scaler["xarray_feature_scale"]["gefs_precip_mm_h"])
    assert center > 10_000_000  # statistics come from the reforecast (training period)
    sample = train[0]
    raw = sample["x_d_forecast"]["gefs_precip_mm_h"][:, 0].numpy() * scale + center
    assert np.isfinite(raw).all() and raw.min() > 10_000_000
    ds = get_dataset(cfg, is_train=False, period="validation", basin=BASINS[0], scaler=train.scaler)
    for item in range(0, len(ds), 997):
        basin, (idx,) = ds.lookup_table[item]
        issue = pd.Timestamp(ds._blocks[basin]["dates"][ds.frequencies[0]][idx - 72])
        raw = ds[item]["x_d_forecast"]["gefs_precip_mm_h"][:, 0].numpy() * scale + center
        if issue >= gefs_init[0] + pd.Timedelta(hours=12):
            pos = gefs_init.searchsorted(issue, side="right") - 1
            assert np.nanmax(raw) < 10_000_000 and int(raw[0]) // 1000 == pos  # operational GEFS takes over once it exists
        elif issue < rf_init[-1]:
            assert np.nanmin(raw) > 10_000_000


def _aorc_forecast_cfg(tmp_path, path, train_end="31/12/2019"):
    basin_file = tmp_path / "basins.txt"
    basin_file.write_text("\n".join(BASINS))
    cfg = Config(dict(
        experiment_name="m", run_dir=str(tmp_path / "run"), data_dir=str(path), dataset="flowcast_zarr",
        train_basin_file=str(basin_file), validation_basin_file=str(basin_file), test_basin_file=str(basin_file),
        train_start_date="01/03/2019", train_end_date=train_end, validation_start_date="01/03/2020", validation_end_date="30/06/2020",
        test_start_date="01/03/2020", test_end_date="30/06/2020", model="handoff_forecast_lstm",
        dynamic_inputs=["aorc_precip_mm_h"], hindcast_inputs=["aorc_precip_mm_h"], forecast_inputs=["aorc_precip_mm_h"],
        nan_handling_method="input_replacing", static_attributes=["area_km2"], target_variables=["qobs_mm_h"],
        seq_length=96, forecast_seq_length=72, predict_last_n=72, hidden_size=8, hindcast_hidden_size=8, forecast_hidden_size=8,
        state_handoff_network={"type": "fc", "hiddens": [8], "activation": "tanh", "dropout": 0.0},
        loss="mse", head="regression", optimizer="Adam", learning_rate=0.001, batch_size=8, epochs=1, device="cpu", verbose=0,
    ))
    cfg.train_dir = tmp_path / "train_data"
    cfg.train_dir.mkdir()
    return cfg


def test_operational_hindcasts_substitute_the_archived_forecast_in_evaluation_only(tmp_path, worker_cube):
    path, _, gefs_init, _ = worker_cube
    cfg = _aorc_forecast_cfg(tmp_path, path, train_end="31/08/2019")
    f = "aorc_precip_mm_h"
    ZarrCubeDataset.configure(DatasetOptions(substitute_forecast={f: "gefs_precip_mm_h"}))
    train = get_dataset(cfg, is_train=True, period="train", scaler={})
    center, scale = (float(train.scaler[k][f]) for k in ("xarray_feature_center", "xarray_feature_scale"))
    assert np.nanmax(train[0]["x_d_forecast"][f][:, 0].numpy() * scale + center) < 1000  # training keeps future AORC
    ds = get_dataset(cfg, is_train=False, period="validation", basin=BASINS[0], scaler=train.scaler)
    item = len(ds) // 2
    basin, (idx,) = ds.lookup_table[item]
    issue = pd.Timestamp(ds._blocks[basin]["dates"][ds.frequencies[0]][idx - 72])
    raw = ds[item]["x_d_forecast"][f][:, 0].numpy() * scale + center
    assert int(raw[0]) // 1000 == gefs_init.searchsorted(issue, side="right") - 1  # the latest GEFS init, normalized like AORC


def test_runs_saved_before_experiment_options_were_removed_still_load():
    saved = {"block_basins": 24, "fill_absent": {}, "mixed_forcing_p": 0.0, "loss_weight": None}
    assert DatasetOptions.from_dict(saved).block_basins == 24
    with pytest.raises(ValueError, match="loss_weight"):
        DatasetOptions.from_dict({"loss_weight": "heat_weight"})
