"""Snow and radiation module: physics checks, terrain factors, interfaces. Offline (synthetic DEM and forcing)."""

import numpy as np
import pandas as pd
import pytest
import shapely
import xarray as xr

from flowcast_pipeline.snow import (
    CLASSIC,
    LSTM_FEATURES,
    NORTHEAST,
    HRUSet,
    MeltMode,
    PrecipSplit,
    band_states,
    basin_features,
    build_hrus,
    forcing_from_cube,
    map_payload,
    run_snow,
    snow_features_for_basin,
    subbasin_states,
)
from flowcast_pipeline.snow import meteo, terrain
from flowcast_pipeline.snow.dem import DEMGrid, global_pixel_to_lonlat, lonlat_to_global_pixel
from flowcast_pipeline.snow.radiation import terrain_shortwave
from flowcast_pipeline.snow.solar import solar_position

ZOOM = 12


def _ridge_dem(n: int = 400, lon0: float = -74.6, lat0: float = 42.2) -> DEMGrid:
    """East-west ridge: north- and south-facing slopes of ~20 degrees rising from 400 m to ~1100 m."""
    px0, py0 = lonlat_to_global_pixel(lon0, lat0, ZOOM)
    grid = DEMGrid(np.zeros((n, n), np.float32), ZOOM, int(px0) - n // 2, int(py0) - n // 2)
    pix = float(np.median(grid.pixel_size_m()))
    rows = np.arange(n)[:, None] * np.ones((1, n))
    dist = np.abs(rows - n / 2) * pix
    grid.elev[:] = (400.0 + np.maximum(0.0, 3000.0 - dist) * np.tan(np.radians(20))).astype(np.float32)
    return grid


def _box(grid: DEMGrid, margin: int) -> shapely.Polygon:
    n = grid.shape[0]
    lon0, lat1 = global_pixel_to_lonlat(grid.px0 + margin, grid.py0 + margin, ZOOM)
    lon1, lat0 = global_pixel_to_lonlat(grid.px0 + n - margin, grid.py0 + n - margin, ZOOM)
    return shapely.box(float(lon0), float(lat0), float(lon1), float(lat1))


@pytest.fixture(scope="module")
def ridge_hrus() -> HRUSet:
    grid = _ridge_dem()
    return build_hrus({"ridge": _box(grid, 60)}, n_bands=2, n_aspects=2, forest_frac=0.0, dem=grid)


def _forcing(start="2019-10-01 01:00", end="2020-06-30 23:00", seed=0, **overrides) -> pd.DataFrame:
    t = pd.date_range(start, end, freq="h")
    rng = np.random.default_rng(seed)
    doy, hr = t.dayofyear.to_numpy(), t.hour.to_numpy()
    temp = -1.0 - 11.0 * np.cos(2 * np.pi * (doy - 20) / 365.0) + 4.0 * np.sin(2 * np.pi * (hr - 14) / 24.0)
    precip = np.where(rng.random(len(t)) < 0.07, rng.gamma(0.8, 2.0, len(t)), 0.0)
    el, _, _ = solar_position(t - pd.Timedelta(minutes=30), 42.2, -74.6)
    frame = pd.DataFrame(
        {
            "precip": precip,
            "air_temperature": temp,
            "surface_pressure": 95000.0,
            "wind_speed": 3.0,
            "shortwave_down": 0.6 * np.maximum(0.0, 1000.0 * np.sin(np.radians(el))),
        },
        index=t,
    )
    for k, v in overrides.items():
        frame[k] = v
    if "specific_humidity" not in overrides:
        frame["specific_humidity"] = 0.8 * meteo.esat_mb(frame["air_temperature"]) * 0.622 / 950.0
    return frame


def test_wet_bulb_and_phase():
    t = np.array([5.0, 2.0, 0.0])
    e = np.array([0.5, 0.4, 1.0]) * meteo.esat_mb(t)
    tw = meteo.wet_bulb(t, e, 100000.0)
    np.testing.assert_allclose(meteo.esat_mb(tw) - 0.00066 * 1000.0 * (t - tw), e, atol=1e-6)
    assert 0.5 < tw[0] < 1.6  # 5 degC at 50% RH
    assert tw[2] == pytest.approx(0.0, abs=1e-6)
    assert np.all(tw <= t)
    frac = meteo.snow_fraction(t, tw, NORTHEAST)
    assert frac[1] == 1.0  # dry 2 degC air: snow by wet-bulb ...
    assert meteo.snow_fraction(t, tw, NORTHEAST.replace(precip_split=PrecipSplit.AIR_TEMPERATURE))[1] == 0.0  # ... rain by Ta


def test_solar_position_summer_solstice_noon():
    el, az, e0 = solar_position(pd.DatetimeIndex(["2020-06-20 16:58"]), 42.0, -75.0)
    assert el[0] == pytest.approx(90 - 42 + 23.44, abs=0.3)
    assert az[0] == pytest.approx(180.0, abs=3.0)
    assert e0[0] == pytest.approx(0.967, abs=0.003)


def test_flat_terrain_factors():
    grid = DEMGrid(np.full((120, 120), 300.0, np.float32), ZOOM, 1_000_000, 1_000_000)
    pixel_m = grid.pixel_size_m()
    slope, aspect = terrain.slope_aspect(grid.elev, pixel_m)
    hor = terrain.horizon_angles(grid.elev, pixel_m, n_dir=8, max_dist_m=500)
    assert np.allclose(slope, 0.0)
    assert np.allclose(terrain.sky_view_factor(hor, slope, aspect), 1.0, atol=1e-3)
    lut = terrain.illumination_lut(np.zeros(100, int), 1, slope.ravel()[:100], aspect.ravel()[:100], hor.reshape(8, -1)[:, :100])
    np.testing.assert_allclose(lut[0], terrain.flat_lut(1)[0], atol=1e-9)


def test_tilted_plane_slope_aspect_and_sky_view():
    n = 200
    grid = DEMGrid(np.zeros((n, n), np.float32), ZOOM, 1_000_000, 1_000_000)
    pixel_m = grid.pixel_size_m()
    y = np.arange(n)[:, None] * pixel_m[:, None]
    grid.elev[:] = (1000.0 - y * np.tan(np.radians(25.0))) * np.ones((1, n))  # falls toward the south
    slope, aspect = terrain.slope_aspect(grid.elev, pixel_m)
    c = slice(50, 150)
    assert np.degrees(slope[c, c]).mean() == pytest.approx(25.0, abs=0.2)
    assert np.degrees(aspect[c, c]).mean() == pytest.approx(180.0, abs=0.5)
    hor = terrain.horizon_angles(grid.elev, pixel_m, n_dir=32, max_dist_m=2000)
    svf = terrain.sky_view_factor(hor, slope, aspect)
    assert svf[c, c].mean() == pytest.approx((1 + np.cos(np.radians(25.0))) / 2, abs=0.01)


def test_ridge_hrus_resolve_aspect(ridge_hrus):
    t = ridge_hrus.table
    assert set(t["aspect_class"]) == {"n", "s"}
    north = t[t["aspect_class"] == "n"]
    south = t[t["aspect_class"] == "s"]
    assert (north["northness"] > 0.1).all() and (south["northness"] < -0.1).all()
    assert t["frac_total"].sum() == pytest.approx(1.0)
    el = int(np.argmin(np.abs(terrain.LUT_ELEVATIONS - 21.0)))
    az = int(np.argmin(np.abs(terrain.LUT_AZIMUTHS - 180.0)))
    k_n = ridge_hrus.lut[north.index, az, el].mean()
    k_s = ridge_hrus.lut[south.index, az, el].mean()
    assert k_s / k_n > 2.0  # low March sun: south slopes get more than twice the beam of north slopes
    assert len(ridge_hrus.geometry["features"]) == t["band_id"].nunique()


def test_hru_save_load_roundtrip(ridge_hrus, tmp_path):
    ridge_hrus.save(tmp_path / "ridge")
    back = HRUSet.load(tmp_path / "ridge")
    pd.testing.assert_frame_equal(back.table, ridge_hrus.table)
    np.testing.assert_allclose(back.lut, ridge_hrus.lut, atol=1e-6)
    assert back.geometry == ridge_hrus.geometry


def test_terrain_shortwave_flat_equals_ghi(ridge_hrus):
    flat = HRUSet(ridge_hrus.table.assign(svf=1.0), terrain.flat_lut(ridge_hrus.n))
    t = pd.date_range("2020-03-15 12:00", periods=12, freq="h")
    ghi = np.full(len(t), 400.0)
    out = terrain_shortwave(t, ghi, flat)
    day = out["ghi_clear"][:, 0] > 50
    np.testing.assert_allclose(out["sw_terrain"][day], 400.0, rtol=0.02)


@pytest.mark.parametrize("params", [NORTHEAST, CLASSIC, NORTHEAST.replace(melt_mode=MeltMode.RADIATION)])
def test_mass_balance_closes(ridge_hrus, params):
    f = _forcing()
    result = run_snow(ridge_hrus, f, params=params)
    ds = result.hru
    water_in = ds["snowfall"].sum("time").to_numpy() + ds["rainfall"].sum("time").to_numpy()
    water_out = ds["rain_plus_melt"].sum("time").to_numpy()
    storage = ds["swe"].isel(time=-1).to_numpy()
    np.testing.assert_allclose(water_in, water_out + storage, rtol=1e-4)
    raw = ds["rainfall"].sum("time").to_numpy() + ds["snowfall"].sum("time").to_numpy() / params.scf
    np.testing.assert_allclose(raw, f["precip"].sum(), rtol=1e-5)
    assert ds["swe"].max() > 50


def test_warm_rain_only_passes_through(ridge_hrus):
    f = _forcing(air_temperature=10.0)
    ds = run_snow(ridge_hrus, f).hru
    assert float(ds["swe"].max()) == 0.0
    np.testing.assert_allclose(ds["rain_plus_melt"].to_numpy(), np.broadcast_to(f["precip"].to_numpy()[:, None], ds["swe"].shape), atol=1e-4)


def test_south_slopes_melt_out_first(ridge_hrus):
    f = _forcing()
    f.loc[f.index >= "2020-03-01", "precip"] = 0.0
    ds = run_snow(ridge_hrus, f).hru
    melt_out = {}
    for h in ds["hru"].to_numpy():
        swe = ds["swe"].sel(hru=h).to_series()
        melt_out[h] = swe[swe > 1.0].index.max()
    for band in (1, 2):
        assert melt_out[f"ridge:b{band}s"] < melt_out[f"ridge:b{band}n"]
    assert melt_out["ridge:b1n"] - melt_out["ridge:b1s"] > pd.Timedelta(days=3)


def test_rain_on_snow_responds_to_wind_and_humidity(ridge_hrus):
    f = _forcing(end="2020-03-10 00:00")
    event = (f.index >= "2020-02-20") & (f.index < "2020-02-21")
    f.loc[event, ["air_temperature", "precip"]] = [8.0, 3.0]
    calm = f.copy()
    windy = f.copy()
    windy.loc[event, "wind_speed"] = 12.0
    windy.loc[event, "specific_humidity"] = 0.99 * meteo.esat_mb(8.0) * 0.622 / 950.0

    def event_melt(frame, params):
        ds = run_snow(ridge_hrus, frame, params=params).hru
        return float(ds["ros_melt"].sel(time=frame.index[event]).sum())

    assert event_melt(windy, NORTHEAST) > 1.5 * event_melt(calm, NORTHEAST)
    assert event_melt(windy, CLASSIC) == pytest.approx(event_melt(calm, CLASSIC), rel=1e-6)


def test_warm_start_matches_continuous_run(ridge_hrus):
    f = _forcing()
    full = run_snow(ridge_hrus, f)
    split = f.index.get_loc(pd.Timestamp("2020-02-01"))
    first = run_snow(ridge_hrus, f.iloc[:split])
    second = run_snow(ridge_hrus, f.iloc[split:], state=first.state)
    np.testing.assert_allclose(second.hru["swe"].to_numpy(), full.hru["swe"].to_numpy()[split:], atol=1e-4)


def test_features_and_map_outputs(ridge_hrus):
    result = run_snow(ridge_hrus, _forcing())
    feats = basin_features(result)
    assert list(feats.columns) == LSTM_FEATURES
    assert feats[[c for c in feats if not c.endswith(("_b3", "_b4"))]].notna().all().all()
    assert feats["snow_cover_frac"].between(0, 1).all()
    assert (feats["snow_line_elev"].between(0, 3000)).all()
    subs = subbasin_states(result)
    assert set(subs["swe"].dims) == {"time", "subbasin"}
    bands = band_states(result)
    assert bands.sizes["band_id"] == 2
    payload = map_payload(result, start="2020-03-01", end="2020-03-01 23:00")
    assert len(payload["times"]) == 24
    assert len(payload["values"]["swe"][0]) == len(payload["geometry"]["features"]) == 2


def test_dataset_step_accepts_aorc_names(ridge_hrus):
    f = _forcing()
    aorc = pd.DataFrame(
        {
            "APCP_surface": f["precip"],
            "TMP_2maboveground": f["air_temperature"] + 273.15,
            "SPFH_2maboveground": f["specific_humidity"],
            "PRES_surface": f["surface_pressure"],
            "UGRD_10maboveground": 3.0,
            "VGRD_10maboveground": 0.0,
            "DSWRF_surface": f["shortwave_down"],
        },
        index=f.index,
    )
    feats = snow_features_for_basin(ridge_hrus, aorc, spinup="2019-11-01")
    assert feats.index[0] >= pd.Timestamp("2019-11-01")
    assert feats["snow_swe"].max() > 50


def test_per_hru_forcing(ridge_hrus):
    f = _forcing(end="2020-01-31 23:00")
    ds = xr.Dataset(
        {k: (("time", "hru"), np.repeat(f[k].to_numpy()[:, None], ridge_hrus.n, axis=1)) for k in f.columns},
        coords={"time": f.index, "hru": ridge_hrus.ids},
    )
    per_hru = run_snow(ridge_hrus, ds).hru
    assert per_hru.sizes["hru"] == ridge_hrus.n
    assert float(per_hru["swe"].max()) > 20


def test_training_cube_adapter(ridge_hrus):
    """One basin of training cube v1 (flowcast_pipeline.dataset): band variables (band, time) plus basin-only ones."""
    f = _forcing()
    n_bands = 2
    offsets = np.array([1.5, -1.5])
    td = meteo.dewpoint_from_vapor_pressure(meteo.vapor_pressure_from_specific_humidity(f["specific_humidity"], 95000.0))
    basin = xr.Dataset(
        {
            "aorc_band_precip_mm_h": (("band", "time"), np.repeat(f["precip"].to_numpy()[None], n_bands, axis=0)),
            "aorc_band_temp_2m_c": (("band", "time"), f["air_temperature"].to_numpy()[None] + offsets[:, None]),
            "aorc_band_dewpoint_2m_c": (("band", "time"), np.asarray(td)[None] + offsets[:, None]),
            "aorc_band_sw_down_wm2": (("band", "time"), np.repeat(f["shortwave_down"].to_numpy()[None], n_bands, axis=0)),
            "aorc_temp_2m_c": (("time",), f["air_temperature"].to_numpy()),
            "aorc_wind_speed_10m": (("time",), np.full(len(f), 4.0)),
            "aorc_pressure_kpa": (("time",), np.full(len(f), 95.0)),
            "band_elev_m": (("band",), np.array([500.0, 1000.0])),
            "band_area_frac": (("band",), np.array([0.5, 0.5])),
        },
        coords={"time": f.index, "band": np.arange(n_bands)},
    )
    forcing, z = forcing_from_cube(basin, ridge_hrus)
    band = ridge_hrus.table["band"].to_numpy()
    np.testing.assert_allclose(z, np.where(band == 1, 500.0, 1000.0))
    t0 = forcing["air_temperature"].isel(time=0).to_numpy()
    np.testing.assert_allclose(t0, f["air_temperature"].iloc[0] + np.where(band == 1, 1.5, -1.5))
    p = forcing["surface_pressure"].isel(time=0).to_numpy()
    assert (p[band == 1] > 95000.0).all() and (p[band == 2] < 95000.0).all()
    feats = snow_features_for_basin(ridge_hrus, forcing, forcing_elevation=z, radiation_label="center")
    assert list(feats.columns) == LSTM_FEATURES
    assert feats["snow_swe_b2"].max() > feats["snow_swe_b1"].max()
