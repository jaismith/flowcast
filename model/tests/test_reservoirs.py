"""NYC reservoir storage inputs (reservoirs.py): source priority, timing, FFMP zones, basin mapping."""

import numpy as np
import pandas as pd
import pytest

from flowcast_model import reservoirs as rs


def _profiles() -> pd.DataFrame:
    doy = np.arange(1, 367)
    curves = {"level1b": 0.95, "level1c": 0.85, "level2": 0.75, "level3": 0.45, "level4": 0.40, "level5": 0.30}
    df = pd.DataFrame({k: np.full(366, v) for k, v in curves.items()}, index=doy)
    for i, zone in enumerate(rs.FFMP_ZONES):
        for r in rs.RESERVOIRS:
            df[f"{zone}_factor_mrf_{r}"] = float(7 - i)
    return df


def _write_sources(tmp_path):
    days = pd.date_range("2021-11-28", "2021-12-03", freq="D")
    dep = pd.DataFrame({"pepacton": 100_000.0, "cannonsville": 60_000.0, "neversink": 20_000.0}, index=days[:3])
    dep.index = dep.index.strftime("%-m/%-d/%Y")
    dep.to_csv(tmp_path / "NYC_storage_daily_2000-2021.csv")
    od = pd.DataFrame(
        {
            "neversink_date": (days[3:5]).strftime("%Y-%m-%dT08:00:00.000"),
            "pepacton_conservation_flow_release": [61.0, 0.0],  # Cannonsville, in billion gallons; 0 is a bad reading
            "cannonsville_release": [101.0, 102.0],  # Pepacton
            "neversink_storage": [21.0, 22.0],
        }
    )
    od.to_csv(tmp_path / "nyc_opendata_reservoirs.csv", index=False)
    usgs = pd.DataFrame({"pepacton": 106_000.0, "cannonsville": 62_000.0, "neversink": 21_500.0}, index=days)
    usgs.index.name = "datetime"
    usgs.to_csv(tmp_path / "usgs_nyc_storage_mg.csv")


def test_daily_storage_prefers_dep_then_open_data_then_offset_usgs(tmp_path):
    _write_sources(tmp_path)
    storage, prov = rs.daily_storage(tmp_path)
    assert prov["usgs_offset_mg"] == {"cannonsville": 2000.0, "pepacton": 6000.0, "neversink": 1500.0}
    assert storage.loc["2021-11-28", "cannonsville"] == 60_000.0
    assert storage.loc["2021-12-01", "cannonsville"] == 61_000.0  # Open Data, remapped column
    assert storage.loc["2021-12-01", "pepacton"] == 101_000.0
    assert storage.loc["2021-12-02", "cannonsville"] == 60_000.0  # the bad 0 falls back to USGS minus its offset
    assert storage.loc["2021-12-03", "neversink"] == 20_000.0


def test_upstream_reservoirs_from_release_gauges():
    assert rs.upstream_reservoirs("01427510", "01417000,01425000") == ["cannonsville", "pepacton"]
    assert rs.upstream_reservoirs("01425000", "") == ["cannonsville"]
    assert rs.upstream_reservoirs("01052500", "01054500") == []


def test_daily_features_zone_factor_and_space():
    idx = pd.date_range("2020-07-01", periods=3, freq="D")
    cap = rs.CAPACITY_MG
    frac = [0.97, 0.80, 0.35]
    storage = pd.DataFrame({r: [f * cap[r] for f in frac] for r in rs.RESERVOIRS}, index=idx)
    df = rs.daily_features(storage, _profiles(), ["cannonsville"], area_km2=1000.0)
    np.testing.assert_allclose(df["res_nyc_frac"], frac)
    np.testing.assert_allclose(df["res_up_frac"], frac)
    # zones: above L1-b -> L1-a (factor 7), between L2 and L1-c -> L1-c (5), between L5 and L4 -> L4 (2)
    np.testing.assert_allclose(df["res_ffmp_factor"], [7.0, 5.0, 2.0])
    np.testing.assert_allclose(df["res_ffmp_margin_l2"], np.array(frac) - 0.75)
    space = (1 - 0.97) * cap["cannonsville"] * rs.MG_TO_M3 / 1e9 * 1000
    assert df["res_up_space_mm"].iloc[0] == pytest.approx(space)


def test_daily_features_missing_storage_is_missing_everywhere():
    idx = pd.date_range("2020-07-01", periods=2, freq="D")
    storage = pd.DataFrame({r: [0.8 * rs.CAPACITY_MG[r], np.nan] for r in rs.RESERVOIRS}, index=idx)
    df = rs.daily_features(storage, _profiles(), ["pepacton"], area_km2=500.0)
    assert df.iloc[0].notna().all()
    assert df.iloc[1].isna().all()


def test_hourly_uses_the_previous_local_day():
    daily = pd.DataFrame({"x": [1.0, 2.0, 3.0]}, index=pd.date_range("2021-07-01", periods=3, freq="D"))
    # hour-ending 2021-07-03 04:00 UTC covers 23:00-24:00 EDT on Jul 2 -> Jul 1's value; 05:00 UTC -> Jul 2's
    times = pd.DatetimeIndex(["2021-07-03T04:00", "2021-07-03T05:00", "2021-07-04T03:00"])
    np.testing.assert_array_equal(rs.hourly(daily, times)[0], [1.0, 2.0, 2.0])
