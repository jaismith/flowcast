"""Water-temperature model pieces: forecast-branch dropout/masking, per-step CMAL masking, daily maxima, cube helpers."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch
import xarray as xr

from flowcast_model.dataset import DatasetOptions
from flowcast_model.hindcast import daily_maxima, sample_mixture
from flowcast_model.models import elementwise_cmal_loss
from flowcast_model.tempcube import forecast_warmup, gauge_temperatures, heat_weight, time_harmonics
from flowcast_model.tempscore import daily_max, diurnal_persistence

from .test_dataset import FORECAST, make_pair


def test_forecast_group_dropout_masks_only_the_forecast_group(tmp_path, cube_path):
    groups = dict(FORECAST, hindcast_inputs=[["precip", "temp", "qobs_shift1"]], forecast_inputs=[["precip"], ["temp"]], nan_handling_method="input_replacing")
    _, ours = make_pair(tmp_path, cube_path, groups, options=DatasetOptions(block_basins=2, optional_inputs=["temp"], forecast_group_dropout={"temp": 1.0}))
    sample = ours[0]
    assert torch.isnan(sample["x_d_forecast"]["temp"]).all()
    assert not torch.isnan(sample["x_d_forecast"]["precip"]).any()
    assert not torch.isnan(sample["x_d_hindcast"]["temp"]).all()


def test_mask_forecast_applies_in_evaluation_only(tmp_path, cube_path):
    groups = dict(FORECAST, hindcast_inputs=[["precip", "temp", "qobs_shift1"]], forecast_inputs=[["precip"], ["temp"]], nan_handling_method="input_replacing")
    _, ours = make_pair(tmp_path, cube_path, groups, period="validation", options=DatasetOptions(block_basins=2, mask_forecast=["temp"]))
    sample = ours[0]
    assert torch.isnan(sample["x_d_forecast"]["temp"]).all()
    assert not torch.isnan(sample["x_d_forecast"]["precip"]).any()


class _Stock:
    """NeuralHydrology's per-window MaskedCMALLoss._get_loss, for comparison."""

    @staticmethod
    def loss(prediction, ground_truth, eps=1e-8):
        mask = ~torch.isnan(ground_truth["y"]).any(1).any(1)
        y, m, b, t, p = (ground_truth["y"][mask], *(prediction[k][mask] for k in ("mu", "b", "tau", "pi")))
        error = y - m
        log_like = torch.log(t) + torch.log(1.0 - t) - torch.log(b) - torch.max(t * error, (t - 1.0) * error) / b
        return -torch.mean(torch.sum(torch.logsumexp(torch.log(p + eps) + log_like, dim=2), dim=1))


def _prediction(B=4, T=6, K=3, seed=0):
    g = torch.Generator().manual_seed(seed)
    return {
        "mu": torch.randn(B, T, K, generator=g),
        "b": torch.rand(B, T, K, generator=g) + 0.1,
        "tau": torch.rand(B, T, K, generator=g) * 0.8 + 0.1,
        "pi": torch.softmax(torch.randn(B, T, K, generator=g), dim=2),
    }


def test_elementwise_cmal_equals_stock_on_complete_windows_and_keeps_gappy_ones():
    loss = elementwise_cmal_loss(SimpleNamespace())
    pred = _prediction()
    y = torch.randn(4, 6, 1)
    assert torch.allclose(loss._get_loss(pred, {"y": y}), _Stock.loss(pred, {"y": y}))
    gappy = y.clone()
    gappy[0, 2, 0] = float("nan")
    value = loss._get_loss(pred, {"y": gappy})
    assert torch.isfinite(value)
    # the gappy window still counts: its complete steps contribute
    full = loss._get_loss(pred, {"y": y})
    assert value != _Stock.loss(pred, {"y": gappy})
    assert abs(float(value) - float(full)) < abs(float(full))


def test_daily_maxima_by_local_date_and_daytime_coverage():
    base = pd.DatetimeIndex(["2021-07-01T12:00"])  # 8 am EDT
    hours = base[0] + pd.to_timedelta(np.arange(1, 181), unit="h")
    local = (hours - pd.Timedelta(hours=1)).tz_localize("UTC").tz_convert("America/New_York")
    values = (local.hour.to_numpy() + 100 * (local.day.to_numpy() - 1)).astype(np.float32)[None, :, None]
    maxima, days, leads = daily_maxima(values, base)
    assert str(days[0, 0]) == "2021-07-01"
    assert maxima[0, 0, 0] == 23  # rest of the issue day
    assert maxima[0, 3, 0] == 323
    # day 7 ends at 8 pm local (180 h after 12Z): daytime covered, maximum is the last hour
    assert maxima[0, 7, 0] == 700 + 19
    assert list(leads) == list(range(maxima.shape[1]))


def test_daily_max_obs_needs_enough_hours():
    idx = pd.date_range("2021-07-01T05:00", periods=48, freq="h", tz="UTC")  # hour-ending, local days start at 05Z
    s = pd.Series(np.arange(48, dtype=float), index=idx)
    s.iloc[30:40] = np.nan
    out = daily_max(s)
    assert list(out.index) == [pd.Timestamp("2021-07-01", tz="UTC")]
    assert out.iloc[0] == 23.0


def test_diurnal_persistence_uses_the_same_hour_of_the_last_observed_day():
    idx = pd.date_range("2021-07-01", periods=24 * 5, freq="h", tz="UTC")
    obs = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    issue = pd.DatetimeIndex([pd.Timestamp("2021-07-03T12:00", tz="UTC")])
    cube = diurnal_persistence(obs, issue, [1, 23, 24, 25], "x")
    got = cube.values[0, :, 0]
    expect = [obs[issue[0] + pd.Timedelta(hours=h - 24 * np.ceil((h + 1) / 24))] for h in (1, 23, 24, 25)]
    np.testing.assert_array_equal(got, expect)


def test_time_harmonics_and_gauge_temperatures():
    times = pd.date_range("2001-01-01T01:00", periods=48, freq="h")
    h = time_harmonics(times, np.array([-75.0, 0.0]))
    assert h["doy_sin"].shape == (2, 48)
    # solar noon at lon 0 is 12 UTC: the mid-hour 11:30-12:30 bracket peaks at -cos
    assert np.argmin(h["solar_cos"][1, :24]) in (11, 12)
    basins = ["01", "02", "03"]
    tw = np.array([np.arange(48.0), np.full(48, 5.0), np.full(48, np.nan)])
    ds = xr.Dataset(
        {
            "tw_c": (("basin", "time"), tw),
            "upstream_slot_site": (("basin", "upstream_slot"), np.array([["02", "", ""], ["", "", ""], ["01", "03", ""]], dtype=object)),
            "upstream_slot_travel_time_h": (("basin", "upstream_slot"), np.array([[8.0, np.nan, np.nan]] * 3)),
            "upstream_slot_area_frac": (("basin", "upstream_slot"), np.array([[0.9, np.nan, np.nan]] * 3)),
            "gauged_outflow_sites": (("basin",), np.array(["", "", "02"], dtype=object)),
        },
        coords={"basin": basins, "time": times},
    )
    derived, flags = gauge_temperatures(ds, [0, 2], pd.Timestamp("2002-01-01"))
    np.testing.assert_array_equal(derived["upstream_tw_c"][0], tw[1])
    np.testing.assert_array_equal(derived["upstream_tw_c"][1], tw[0])  # slot 1 ("03") has no temperature
    assert np.isnan(derived["outflow_tw_c"][0]).all() and (derived["outflow_tw_c"][1] == 5.0).all()
    assert list(flags["has_upstream_tw"]) == [1.0, 1.0] and list(flags["has_outflow_tw"]) == [0.0, 1.0]


def test_coherent_samples_keep_marginals_and_rank_paths():
    torch.manual_seed(0)
    B, T, K = 2, 5, 3
    pred = {
        "mu": torch.randn(B, T, K),
        "b": torch.rand(B, T, K) + 0.2,
        "tau": torch.rand(B, T, K) * 0.6 + 0.2,
        "pi": torch.softmax(torch.randn(B, T, K), dim=2),
    }
    pos = torch.arange(T)
    ind = sample_mixture(pred, "cmal", pos, K, 20000)
    coh = sample_mixture(pred, "cmal", pos, K, 20000, coherent=True)
    for q in (0.1, 0.5, 0.9):
        np.testing.assert_allclose(torch.quantile(coh, q, dim=2).numpy(), torch.quantile(ind, q, dim=2).numpy(), rtol=0.05, atol=0.05)
    # a coherent path stays at one quantile level: ranks across steps are strongly correlated
    r = coh[0].argsort(dim=1).argsort(dim=1).float()
    assert np.corrcoef(r.numpy())[0, 1] > 0.9
    r = ind[0].argsort(dim=1).argsort(dim=1).float()
    assert abs(np.corrcoef(r.numpy())[0, 1]) < 0.05


def test_weighted_elementwise_cmal_reduces_to_unweighted_with_unit_weights_and_upweights_steps():
    pred = _prediction()
    y = torch.randn(4, 6, 1)
    y[1, 3, 0] = float("nan")
    plain = elementwise_cmal_loss(SimpleNamespace(_ground_truth_keys=["y"]))
    weighted = elementwise_cmal_loss(SimpleNamespace(_ground_truth_keys=["y"]), weighted=True)
    assert weighted._ground_truth_keys == ["y", "loss_weight"]
    ones = torch.ones_like(y)
    assert torch.allclose(weighted._get_loss(pred, {"y": y, "loss_weight": ones}), plain._get_loss(pred, {"y": y}))
    # scale-free: doubling every weight changes nothing; weighting one step pulls the loss towards that step's term
    assert torch.allclose(weighted._get_loss(pred, {"y": y, "loss_weight": 2 * ones}), plain._get_loss(pred, {"y": y}))
    heavy = ones.clone()
    heavy[0, 0, 0] = 1000.0
    only = y.clone()
    only[:] = float("nan")
    only[0, 0, 0] = y[0, 0, 0]
    n_valid = int((~torch.isnan(y)).sum())
    target = plain._get_loss(pred, {"y": only}) * n_valid / 4
    assert abs(float(weighted._get_loss(pred, {"y": y, "loss_weight": heavy})) - float(target)) < 0.05 * abs(float(target)) + 0.05


def test_beta_nll_weights_steps_by_predicted_scale_without_moving_its_gradient():
    pred = _prediction()
    y = torch.randn(4, 6, 1)
    y[2, 1, 0] = float("nan")
    plain = elementwise_cmal_loss(SimpleNamespace(_ground_truth_keys=["y"]))
    beta = elementwise_cmal_loss(SimpleNamespace(_ground_truth_keys=["y"]), beta=1.0)
    assert torch.allclose(elementwise_cmal_loss(SimpleNamespace(_ground_truth_keys=["y"]), beta=0.0)._get_loss(pred, {"y": y}), plain._get_loss(pred, {"y": y}))
    # equal predicted scales everywhere: the weights are all 1
    flat = {**pred, "b": torch.full_like(pred["b"], 0.7)}
    assert torch.allclose(beta._get_loss(flat, {"y": y}), plain._get_loss(flat, {"y": y}))
    # the location gradient of each step is its plain gradient times its (normalized) scale weight
    mu = pred["mu"].clone().requires_grad_(True)
    beta._get_loss({**pred, "mu": mu}, {"y": y}).backward()
    g_beta = mu.grad.clone()
    mu.grad = None
    plain._get_loss({**pred, "mu": mu}, {"y": y}).backward()
    w = (pred["pi"] * pred["b"]).sum(dim=2)
    valid = ~torch.isnan(y[..., 0])
    w = w * valid / (w * valid).sum() * valid.sum()
    torch.testing.assert_close(g_beta, mu.grad * w[..., None])


def test_loss_weight_sample_is_the_raw_series_aligned_with_y(tmp_path, cube_path):
    _, ours = make_pair(tmp_path, cube_path, FORECAST, options=DatasetOptions(block_basins=2, optional_inputs=["temp"], loss_weight="qobs"))
    sample = ours[0]
    assert sample["loss_weight"].shape == sample["y"].shape
    center = float(ours.scaler["xarray_feature_center"]["qobs"].values)
    scale = float(ours.scaler["xarray_feature_scale"]["qobs"].values)
    ok = ~torch.isnan(sample["y"])
    np.testing.assert_allclose(sample["loss_weight"][ok].numpy(), (sample["y"][ok] * scale + center).numpy(), rtol=1e-4, atol=1e-5)


def test_forecast_warmup_is_zero_on_the_first_day_and_tracks_the_running_window():
    leads = np.arange(0, 75, 3, dtype=float)
    temp = np.where(leads <= 24, 10.0, 10.0 + (leads - 24) / 6.0)  # flat first day, then +4 degC per day
    da = xr.DataArray(temp[None, None, None, :].astype(np.float32), dims=("basin", "gefs_init", "gefs_member", "gefs_lead"), coords={"gefs_lead": leads})
    warm = forecast_warmup(da, "gefs_lead")
    wmax = warm["warmup_max"].values[0, 0, 0]
    assert wmax.shape == leads.shape and np.allclose(wmax[leads <= 24], 0.0)
    assert np.isclose(wmax[leads == 48][0], 4.0) and np.isclose(wmax[leads == 72][0], 8.0)
    wmean = warm["warmup_mean"].values[0, 0, 0]
    assert 0.0 < wmean[leads == 48][0] < wmax[leads == 48][0]


def test_heat_weight_marks_warm_ups_and_heat_wave_onsets_in_summer_only():
    times = pd.date_range("2001-01-01T05:00", "2021-12-31T04:00", freq="h")
    local_day = (times - pd.Timedelta(hours=1)).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None).normalize()
    days = pd.DatetimeIndex(local_day)
    base = 20.0 + 8.0 * np.sin(2 * np.pi * (days.dayofyear.to_numpy() - 110) / 365.25)
    rng = np.random.default_rng(0)
    noise = pd.Series(rng.normal(0, 1.0, len(np.unique(days))), index=np.unique(days)).reindex(days).to_numpy()
    air = base + noise
    wave = (days >= "2021-07-10") & (days < "2021-07-15")
    air = np.where(wave, base + 12.0, air)
    winter = (days >= "2021-01-10") & (days < "2021-01-15")
    air = np.where(winter, base + 12.0, air)
    w = heat_weight(air[None, :].astype(np.float32), times, pd.Timestamp("2019-10-01"))[0]
    day_w = pd.Series(w, index=days).groupby(level=0).max()
    assert day_w["2021-07-10":"2021-07-12"].eq(3.0).all()
    assert day_w["2021-01-10":"2021-01-14"].eq(1.0).all()
    assert day_w.min() >= 1.0 and day_w.max() <= 3.0
    assert (day_w > 1.0).mean() < 0.25
