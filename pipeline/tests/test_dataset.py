import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp
from shapely.geometry import Point, box

from flowcast_pipeline.dataset import extract, regulation, sources, weights
from flowcast_pipeline.dataset.grids import Grid, GEOGRAPHIC
from flowcast_pipeline.dataset.targets import hourly_mean

GRID = Grid("toy", GEOGRAPHIC.to_wkt(), 0.05, 0.1, 40, 0.05, 0.1, 30, 8, 8)


def test_hourly_mean_is_hour_ending():
    t = pd.to_datetime(["2020-01-01T00:15Z", "2020-01-01T00:30Z", "2020-01-01T01:00Z", "2020-01-01T01:15Z"])
    iv = pd.DataFrame({"time": t, "value": [1.0, 2.0, 3.0, 10.0], "approval_status": ["Approved", "Approved", "Provisional", "Approved"]})
    out = hourly_mean(iv).set_index("time")
    assert out.loc[pd.Timestamp("2020-01-01T01:00Z"), "value"] == pytest.approx(2.0)
    assert out.loc[pd.Timestamp("2020-01-01T01:00Z"), "n_obs"] == 3
    assert out.loc[pd.Timestamp("2020-01-01T01:00Z"), "approved_frac"] == pytest.approx(2 / 3)
    assert out.loc[pd.Timestamp("2020-01-01T02:00Z"), "value"] == 10.0


def test_basin_weights_match_polygon_area():
    poly = gpd.GeoSeries([box(0.33, 0.47, 1.71, 2.22)], crs="EPSG:4326")
    w = weights.basin_weights(poly, GRID, supersample=20)
    # Cells are 0.1 degree; weights include cos(lat), so compare against the cos-weighted area.
    expected = (1.71 - 0.33) * (2.22 - 0.47) / 0.01 * np.cos(np.radians(1.345))
    assert w.sum() == pytest.approx(expected, rel=0.01)


def test_band_weights_split_equal_area_by_elevation():
    w = sp.csr_matrix(np.ones((1, 8)))
    elev = np.array([10, 80, 20, 70, 30, 60, 40, 50], dtype=float)
    wb, share = weights.band_weights(w, elev, 4)
    assert share == pytest.approx(np.full((1, 4), 0.25))
    lowest = set(wb[0].indices)
    assert lowest == {0, 2}


def test_dewpoint_conversions_agree():
    t, rh = np.array([20.0]), np.array([50.0])
    td = sources.dewpoint_from_rh(t, rh)
    assert td[0] == pytest.approx(9.3, abs=0.2)
    e = 6.112 * np.exp(17.67 * td / (td + 243.5)) * 100
    q = 0.622 * e / (101325 - 0.378 * e)
    assert sources.dewpoint_from_q(q, np.array([101325.0]))[0] == pytest.approx(td[0], abs=0.3)


def _dams(rows):
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


def test_below_dam_requires_storage_drainage_and_distance():
    dam = {"nid_id": "D1", "lat": 42.0, "lon": -75.0, "drainage_km2": 900.0, "normal_storage_af": 300_000.0, "nid_storage_af": 400_000.0, "major": True}
    near = regulation.below_dam(_dams([dam]), area_km2=1000.0, gauge_lat=42.01, gauge_lon=-75.0)
    assert near["below_dam"] == 1.0 and near["below_dam_nid_id"] == "D1"
    small = regulation.below_dam(_dams([dam | {"drainage_km2": 100.0}]), 1000.0, 42.01, -75.0)
    assert small["below_dam"] == 0.0
    far = regulation.below_dam(_dams([dam]), 1000.0, 43.0, -75.0)
    assert far["below_dam"] == 0.0


def test_outflow_gauges_pick_gauge_nearest_each_dam_and_drop_nested():
    polys = gpd.GeoSeries(
        {"A": box(0, 0, 1, 1), "A2": box(0, 0, 1.3, 1.3), "B": box(2, 0, 3, 1), "C": box(0, 0, 3.2, 1.4)}, crs="EPSG:5070"
    )
    polys = gpd.GeoDataFrame(geometry=polys)
    areas = polys.area
    table = pd.DataFrame(
        {
            "below_dam": [1.0, 1.0, 1.0, 0.0],
            "below_dam_nid_id": ["D1", "D1", "D2", ""],
            "below_dam_drainage_frac": [0.95, 0.6, 0.9, 0.0],
        },
        index=["A", "A2", "B", "C"],
    )
    assert regulation.outflow_gauges(["A", "A2", "B"], table, polys, areas) == ["A", "B"]


def test_accumulator_weighted_mean_with_missing_cells():
    tile = extract.Tile(0, 0, 1, 0, 3, np.arange(3), np.array([0]), sp.csr_matrix(np.array([[1.0, 1.0, 2.0]], dtype=np.float32)), np.array([4.0], dtype=np.float32))
    plan = extract.Plan("aorc", ["u"], np.array([0]), np.array([4.0], dtype=np.float32), [tile], {0}, [extract.Shard("s", 2020, 0, 2, np.zeros(2))])
    acc = extract.ShardAccumulator(plan, 0)
    x = np.array([[1.0, 3.0, np.nan], [1.0, 3.0, 5.0]], dtype=np.float32)
    num = extract._reduce(np.nan_to_num(x), tile)[..., None].repeat(8, axis=-1)
    den = extract._reduce((~np.isnan(x)).astype(np.float32), tile)[..., None].repeat(8, axis=-1)
    acc.add(0, 0, num, den)
    vals = acc.values(np.array([0]))[0, :, 0]
    assert vals[0] == pytest.approx(2.0)  # only the two valid cells (weight 2 of 4 = exactly the 50% threshold)
    assert vals[1] == pytest.approx((1 + 3 + 10) / 4)


def test_accumulator_masks_when_under_half_valid():
    tile = extract.Tile(0, 0, 1, 0, 2, np.arange(2), np.array([0]), sp.csr_matrix(np.array([[1.0, 3.0]], dtype=np.float32)), np.array([4.0], dtype=np.float32))
    plan = extract.Plan("aorc", ["u"], np.array([0]), np.array([4.0], dtype=np.float32), [tile], {0}, [extract.Shard("s", 2020, 0, 1, np.zeros(1))])
    acc = extract.ShardAccumulator(plan, 0)
    x = np.array([[1.0, np.nan]], dtype=np.float32)
    num = extract._reduce(np.nan_to_num(x), tile)[..., None].repeat(8, axis=-1)
    den = extract._reduce((~np.isnan(x)).astype(np.float32), tile)[..., None].repeat(8, axis=-1)
    acc.add(0, 0, num, den)
    assert np.isnan(acc.values(np.array([0]))[0, 0, 0])


def test_grid_index_range_handles_descending_axes():
    g = Grid("desc", GEOGRAPHIC.to_wkt(), 0.05, 0.1, 10, 0.95, -0.1, 10, 5, 5)
    assert g.index_range(0.42, 0.58, "y") == (4, 6)
    assert g.index_range(0.0, 0.09, "x") == (0, 1)


def test_point_outside_ring_not_admitted():
    basins = gpd.GeoDataFrame({"STAID": ["B"]}, geometry=[box(-75.1, 41.9, -74.9, 42.1)], crs="EPSG:4326").set_index("STAID")
    dams = _dams(
        [
            {"nid_id": "IN", "lat": 42.0, "lon": -75.0, "drainage_km2": 10.0, "normal_storage_af": 1.0, "nid_storage_af": 1.0, "major": False},
            {"nid_id": "RING", "lat": 42.11, "lon": -75.0, "drainage_km2": 400.0, "normal_storage_af": 1e5, "nid_storage_af": 1e5, "major": True},
            {"nid_id": "RING_SMALL", "lat": 42.11, "lon": -75.0, "drainage_km2": 5.0, "normal_storage_af": 1e3, "nid_storage_af": 1e3, "major": False},
        ]
    )
    joined = regulation.dams_in_basins(dams, basins, pd.Series({"B": 500.0}))
    assert set(joined["nid_id"]) == {"IN", "RING"}
    assert Point(-75.0, 42.11).distance(basins.geometry.iloc[0]) > 0
