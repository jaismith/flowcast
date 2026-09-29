import numpy as np
import pandas as pd
import pytest

from flowcast_pipeline.dataset import reservoirs
from flowcast_pipeline.dataset.reservoirs import DamSeries


def test_daily_value_is_used_the_next_local_day():
    index = pd.date_range("2020-01-02T00:00", "2020-01-04T00:00", freq="h", tz="UTC")
    daily = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]))
    h = pd.Series(reservoirs.daily_to_hourly(daily, index), index=index)
    # Hour ending 2020-01-02 06:00 UTC covers 00:00-01:00 EST on Jan 2: Jan 1's value.
    assert h["2020-01-02T06:00"] == 1.0
    # Hour ending 05:00 UTC Jan 2 is still Jan 1 local (23:00-24:00 EST): Dec 31's value, missing.
    assert np.isnan(h["2020-01-02T05:00"])
    assert h["2020-01-03T12:00"] == 2.0


def test_subdaily_is_hour_ending_mean():
    idx = pd.date_range("2020-01-01T00:15", periods=8, freq="15min", tz="UTC")
    s = pd.Series(np.arange(8, dtype=float), index=idx)
    index = pd.date_range("2020-01-01T01:00", periods=2, freq="h", tz="UTC")
    np.testing.assert_allclose(reservoirs.subdaily_to_hourly(s, index), [1.5, 5.5])


def test_backfill_only_with_a_live_release():
    n = 4
    s = DamSeries(fill=np.full(n, np.nan, dtype=np.float32), release_gauge=np.array([np.nan, 2, 2, 2], dtype=np.float32),
                  release_backfill=np.array([1, 1, np.nan, np.nan], dtype=np.float32))
    np.testing.assert_array_equal(s.release(gauge_ok=True), [1, 2, 2, 2])
    assert s.release(gauge_ok=False) is None


def test_pool_index_uses_pre_test_percentiles():
    elev = np.linspace(100, 110, 24 * 400).astype(np.float32)
    before = np.arange(elev.size) < elev.size // 2
    idx = reservoirs.pool_index(elev, before)
    lo, hi = np.percentile(elev[before], reservoirs.POOL_PCT)
    assert idx[before].min() == pytest.approx((elev[0] - lo) / (hi - lo))
    assert idx[-1] > 1.0


def test_basin_features_weight_fill_and_skip_nested_releases():
    n = 30
    dams = pd.DataFrame({"capacity_af": [3000.0, 1000.0]}, index=["UP", "DOWN"])
    pairs = pd.DataFrame({"nid_id": ["UP", "DOWN"], "STAID": ["B", "B"]})
    fill_up = np.full(n, 0.5, dtype=np.float32)
    fill_down = np.full(n, 1.0, dtype=np.float32)
    fill_down[:10] = np.nan
    rel_up = np.full(n, 10.0, dtype=np.float32)
    rel_down = np.full(n, 30.0, dtype=np.float32)
    rel_down[:5] = np.nan
    series = {"UP": DamSeries(fill=fill_up, release_cwms=rel_up), "DOWN": DamSeries(fill=fill_down, release_cwms=rel_down)}
    network = pd.DataFrame({"comid": [1, 2], "down": [[1, 2, 3], [2, 3]], "gauge": ["", ""], "gauge_comid": [None, None]}, index=["UP", "DOWN"])
    out = reservoirs.basin_features(["B", "EMPTY"], np.array([100.0, 50.0]), pairs, dams, series, network, {}, n)
    assert out["res_fill"][0, 0] == pytest.approx(0.5) and out["res_fill_cov"][0, 0] == pytest.approx(0.75)
    assert out["res_fill"][0, 20] == pytest.approx((3000 * 0.5 + 1000 * 1.0) / 4000)
    # UP's release is inside DOWN's once DOWN reports: only DOWN is summed then.
    assert out["res_release_mm_h"][0, 0] == pytest.approx(10.0 * 3.6 / 100.0)
    assert out["res_release_mm_h"][0, 10] == pytest.approx(30.0 * 3.6 / 100.0)
    assert out["res_release_cov"][0, 10] == pytest.approx(1.0) and out["res_release_cov"][0, 0] == pytest.approx(0.75)
    assert np.isnan(out["res_fill"][1]).all() and (out["res_fill_cov"][1] == 0).all() and (out["res_release_avail"][1] == 0).all()


def test_dams_sharing_a_gauge_are_counted_once():
    n = 3
    dams = pd.DataFrame({"capacity_af": [5000.0, 2000.0]}, index=["A", "B"])
    pairs = pd.DataFrame({"nid_id": ["A", "B"], "STAID": ["X", "X"]})
    q = np.full(n, 8.0, dtype=np.float32)
    series = {d: DamSeries(fill=np.full(n, np.nan, dtype=np.float32), release_gauge=q, gauge_site="G") for d in ("A", "B")}
    network = pd.DataFrame({"comid": [1, 2], "down": [[1, 9], [2, 9]], "gauge": ["G", "G"], "gauge_comid": [9, 9]}, index=["A", "B"])
    out = reservoirs.basin_features(["X"], np.array([36.0]), pairs, dams, series, network, {"X": {1, 2, 9}}, n)
    assert out["res_release_mm_h"][0, 0] == pytest.approx(8.0 * 3.6 / 36.0)
    assert out["res_release_cov"][0, 0] == pytest.approx(1.0)


def test_dam_and_its_dikes_are_one_reservoir():
    table = pd.DataFrame({"name": ["Wachusett North Dike", "Wachusett Reservoir Dam", "Other Dam"], "capacity_af": [187000.0, 187000.0, 187000.0],
                          "lat": [42.40, 42.403, 44.0], "lon": [-71.717, -71.688, -70.0]}, index=["D1", "MAIN", "FAR"])
    assert reservoirs.same_reservoir(table) == {"D1": "MAIN"}


def test_release_gauge_counts_only_upstream_of_the_basin():
    n = 5
    dams = pd.DataFrame({"capacity_af": [2000.0]}, index=["D"])
    pairs = pd.DataFrame({"nid_id": ["D", "D"], "STAID": ["G1", "B2"]})
    series = {"D": DamSeries(fill=np.full(n, np.nan, dtype=np.float32), release_gauge=np.full(n, 5.0, dtype=np.float32))}
    network = pd.DataFrame({"comid": [1], "down": [[1, 7]], "gauge": ["G1"], "gauge_comid": [7]}, index=["D"])
    out = reservoirs.basin_features(["G1", "B2"], np.array([10.0, 20.0]), pairs, dams, series, network, {"B2": {7, 1}}, n)
    assert (out["res_release_avail"][0] == 0).all()  # the gauge is basin G1's own gauge
    assert out["res_release_mm_h"][1, 0] == pytest.approx(5.0 * 3.6 / 20.0)
