import numpy as np
import pandas as pd
import pytest

from flowcast_eval.baselines import Air2Stream, Climatology, climatology, daily_persistence, fit_recession, persistence, recession_persistence
from flowcast_eval.baselines.air2stream import simulate
from flowcast_eval.baselines.persistence import RecessionCurve


def test_persistence_uses_only_data_available_at_issue_time():
    idx = pd.date_range("2025-01-01", periods=48, freq="h", tz="UTC")
    obs = pd.Series(np.arange(48, dtype=float), index=idx)
    issues = pd.DatetimeIndex([pd.Timestamp("2025-01-01T12:00Z")])
    cube = persistence(obs, issues, [1, 6], "USGS-1", latency_h=1.0)
    # obs at 12:00 is not yet published at 12:00; the 11:00 value is.
    assert cube.values[0, :, 0].tolist() == [11.0, 11.0]


def test_persistence_goes_missing_when_obs_are_stale():
    idx = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    obs = pd.Series([1.0, 2.0, 3.0], index=idx)
    issues = pd.DatetimeIndex([pd.Timestamp("2025-01-02T00:00Z")])
    assert np.isnan(persistence(obs, issues, [1], "USGS-1", max_age_h=6).values).all()


def test_recession_fit_recovers_synthetic_curve():
    true = RecessionCurve(a=2e-4, b=1.3, q_floor=0.0)
    # Repeated storms: jump up every 20 days, then recede along the true curve.
    q = np.concatenate([true.project(np.array([q0]), np.arange(24 * 20))[0] for q0 in np.tile([3000.0, 8000.0, 20000.0, 5000.0], 5)])
    obs = pd.Series(q, index=pd.date_range("2001-01-01", periods=len(q), freq="h", tz="UTC"))
    fit = fit_recession(obs)
    assert fit.b == pytest.approx(1.3, abs=0.1)


def test_recession_persistence_recedes_only_on_falling_limb():
    idx = pd.date_range("2025-01-01", periods=24, freq="h", tz="UTC")
    falling = pd.Series(np.linspace(5000, 4000, 24), index=idx)
    rising = pd.Series(np.linspace(4000, 5000, 24), index=idx)
    curve = RecessionCurve(a=1e-3, b=1.0, q_floor=100.0)
    issues = pd.DatetimeIndex([idx[-1] + pd.Timedelta(hours=1)])
    down = recession_persistence(falling, issues, [24, 48], "USGS-1", curve).values[0, :, 0]
    flat = recession_persistence(rising, issues, [24, 48], "USGS-1", curve).values[0, :, 0]
    assert down[0] < 4000 and down[1] < down[0]
    assert flat.tolist() == [5000.0, 5000.0]


def test_climatology_quantiles_follow_season():
    idx = pd.date_range("2001-01-01", "2010-12-31 23:00", freq="h", tz="UTC")
    obs = pd.Series(10 + 5 * np.sin(2 * np.pi * (idx.dayofyear - 100) / 365), index=idx)
    clim = Climatology.fit(obs, n_quantiles=10)
    issues = pd.DatetimeIndex([pd.Timestamp("2023-01-01T00:00Z"), pd.Timestamp("2023-07-01T00:00Z")])
    cube = climatology(clim, issues, [24], "USGS-1", "discharge")
    assert cube.kind == "quantiles" and cube.values.shape == (2, 1, 10)
    assert cube.values[1, 0].mean() > cube.values[0, 0].mean()
    assert np.all(np.diff(cube.values[0, 0]) >= 0)


def test_daily_persistence_uses_previous_day():
    days = pd.date_range("2025-07-01", periods=5, freq="D")
    obs = pd.Series([20.0, 21.0, 22.0, 23.0, 24.0], index=days)
    issues = pd.DatetimeIndex([pd.Timestamp("2025-07-04T12:00Z")])
    cube = daily_persistence(obs, issues, [0, 1], "USGS-1", "water_temperature_daily_max")
    assert cube.values[0, :, 0].tolist() == [22.0, 22.0]


def test_air2stream_recovers_skill_on_synthetic_data():
    days = pd.date_range("2005-01-01", periods=3 * 365, freq="D")
    doy = days.dayofyear.to_numpy()
    rng = np.random.default_rng(0)
    ta = pd.Series(12 + 12 * np.sin(2 * np.pi * (doy - 110) / 365) + rng.normal(0, 3, len(days)), index=days)
    q = pd.Series(np.exp(rng.normal(7, 0.5, len(days))), index=days)
    a = np.array([0.5, 0.2, 0.25, 0.3, 0.1, 0.5, 0.6, 0.05])
    tw = pd.Series(simulate(a, ta.to_numpy(), (q / q.mean()).to_numpy(), doy, 4.0), index=days)
    model = Air2Stream.fit(tw, ta, q, maxiter=60)
    assert model.rmse_train < 0.5

    issues = pd.DatetimeIndex(days[400:410]).tz_localize("UTC") + pd.Timedelta(hours=12)
    cube = model.forecast(tw, ta, q, issues, [0, 1, 2], "USGS-1")
    truth = np.stack([tw.reindex(d.tz_convert(None).floor("D") + pd.to_timedelta([0, 1, 2], unit="D")).to_numpy() for d in issues])
    assert np.sqrt(np.nanmean((cube.values[:, :, 0] - truth) ** 2)) < 1.0
