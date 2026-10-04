"""Water-temperature model pieces: forecast-branch dropout/masking, per-step CMAL masking, daily maxima, cube helpers."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch
import xarray as xr

from flowcast_eval.protocol import HOURLY_LEADS_H
from flowcast_model.dataset import DatasetOptions
from flowcast_model.hindcast import daily_maxima, sample_mixture
from flowcast_model.models import elementwise_cmal_loss
from flowcast_model.tempcube import gauge_temperatures, time_harmonics
from flowcast_model.tempscore import daily_max, diurnal_persistence, load_forecasts

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


def test_load_forecasts_keeps_the_harness_leads_of_all_leads_hindcasts(tmp_path):
    issue = pd.Timestamp("2021-07-01T12:00", tz="UTC")
    rows = [("water_temperature", h) for h in range(1, 181)] + [("water_temperature_daily_max", 24.0 * d) for d in range(8)]
    frame = pd.DataFrame({"variable": [v for v, _ in rows], "lead_h": [float(h) for _, h in rows]})
    frame = frame.assign(model="lstm_temp_v2", issue_time=issue, valid_time=issue + pd.to_timedelta(frame["lead_h"], unit="h"), value=20.0, unit="degC", run_type="operational", member=0)
    (tmp_path / "site_id=USGS-1").mkdir()
    frame.to_parquet(tmp_path / "site_id=USGS-1" / "lstm_temp_v2.parquet")
    got = load_forecasts({"v2": [tmp_path]}, "USGS-1")
    assert sorted(got.loc[got["variable"] == "water_temperature", "lead_h"]) == [h for h in HOURLY_LEADS_H if h <= 180]
    assert (got["variable"] == "water_temperature_daily_max").sum() == 8


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
