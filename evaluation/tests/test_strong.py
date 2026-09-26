import numpy as np
import pandas as pd
import pytest

from flowcast_pipeline.sites import get_site
from flowcast_eval.metrics import score
from flowcast_eval.pairs import pairs_from_cube
from flowcast_eval.precip import forecast_precip, inside, observed_precip_windows
from flowcast_eval.strong import ARX, GBM, Inputs, Routing, features

SITE = get_site("01427510")


def test_inside_polygon():
    square = np.array([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]], float)
    assert inside(square, np.array([1.0, 3.0, 0.5]), np.array([1.0, 1.0, 1.9])).tolist() == [True, False, True]


def test_forecast_precip_uses_latest_init_available_at_issue_time():
    leads = [str(h) for h in range(0, 217, 3)]
    qpf = pd.DataFrame([[0.0] + [1.0] * (len(leads) - 1)], index=pd.DatetimeIndex([pd.Timestamp("2021-01-01T00:00Z")]), columns=leads)
    issues = pd.DatetimeIndex(["2021-01-01T12:00Z", "2021-01-01T03:00Z", "2021-01-02T02:00Z"])
    out = forecast_precip(qpf, issues, [(0, 24), (0, 6)])
    assert out[0].tolist() == pytest.approx([8.0, 2.0])  # 1 mm per 3 h from the 00Z init, 12 h in
    assert np.isnan(out[1]).all()  # 00Z GEFS isn't available until 06Z
    assert out[2].tolist() == pytest.approx([8.0, 2.0])


def test_observed_precip_windows_floor_to_the_hour():
    idx = pd.date_range("2021-01-01", periods=48, freq="h", tz="UTC")
    p = pd.Series(1.0, index=idx)
    out = observed_precip_windows(p, pd.DatetimeIndex(["2021-01-01T12:45Z"]), [(-7, -1), (-1, 5)])
    assert out[0].tolist() == [6.0, 6.0]


def synthetic_inputs(n_days: int = 900, seed: int = 0, travel_h: int = 8) -> Inputs:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2016-01-01", periods=24 * n_days, freq="h", tz="UTC")
    rain = np.where(rng.random(len(idx)) < 0.01, rng.gamma(2.0, 3.0, len(idx)), 0.0)
    kernel = np.exp(-np.arange(96) / 24.0)
    upstream = 300.0 + 200.0 * np.convolve(rain, kernel)[: len(idx)]
    q = 500.0 + 1.2 * np.concatenate([np.full(travel_h, upstream[0]), upstream[:-travel_h]]) + 50.0 * np.convolve(rain, kernel / 4)[: len(idx)]
    return Inputs(SITE, pd.Series(q, index=idx), {"USGS-UP": pd.Series(upstream, index=idx)}, pd.Series(rain, index=idx), pd.DataFrame())


def _issues(inp: Inputs, start: str, end: str) -> pd.DatetimeIndex:
    hours = pd.date_range(start, end, freq="6h", tz="UTC")
    return hours[(hours >= inp.index[200]) & (hours <= inp.index[-24 * 9])]


def test_operational_features_ignore_everything_after_the_last_observation():
    inp = synthetic_inputs(n_days=60)
    issue = pd.DatetimeIndex([pd.Timestamp("2016-02-01T12:00Z")])
    before = features(inp, issue, 24.0, "qpf")
    cutoff = issue[0] - pd.Timedelta(hours=1)

    def tamper(s: pd.Series, value: float) -> pd.Series:
        return s.where(s.index <= cutoff, value)

    changed = Inputs(SITE, tamper(inp.q, 1e6), {"USGS-UP": tamper(inp.upstream["USGS-UP"], 1e6)}, tamper(inp.precip, 99.0), pd.DataFrame())
    after = features(changed, issue, 24.0, "qpf")
    pd.testing.assert_frame_equal(before, after)


def test_routing_recovers_the_travel_time():
    inp = synthetic_inputs(travel_h=8)
    train = _issues(inp, "2016-01-10", "2017-12-31")
    model = Routing.fit(inp, train, [6.0, 12.0, 24.0], sweeps=1)
    assert abs(model.travel_h["USGS-UP"] - 8) <= 2
    test = _issues(inp, "2018-01-01", "2018-06-01")
    routed = pairs_from_cube(model.forecast(inp, test, [6.0]), inp.q)
    persist = inp.q.reindex(test - pd.Timedelta(hours=1)).to_numpy()
    assert np.nanmean(routed["crps"]) < 0.5 * np.nanmean(np.abs(persist - routed["obs"].to_numpy()))


def test_arx_and_gbm_beat_persistence_with_perfect_precip():
    inp = synthetic_inputs()
    train = _issues(inp, "2016-01-10", "2017-12-31")
    test = _issues(inp, "2018-01-01", "2018-06-01")
    leads = [12.0, 48.0]
    arx = ARX.fit(inp, train, leads)
    gbm = GBM.fit(inp, train, leads, holdout_start="2017-09-01")
    for cube in (arx.forecast(inp, test, leads, "obs"), gbm.forecast(inp, test, leads, "obs")):
        pairs = pairs_from_cube(cube, inp.q)
        at48 = pairs[pairs["lead_h"] == 48.0]
        persist = inp.q.reindex(at48["issue_time"] - pd.Timedelta(hours=1)).to_numpy()
        assert score(at48["point"].to_numpy(), at48["obs"].to_numpy())["mae"] < score(persist, at48["obs"].to_numpy())["mae"]
    assert cube.model == "lgbm_obs_precip" and cube.run_type == "perfect_forcing"
