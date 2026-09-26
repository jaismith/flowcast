import numpy as np
import pandas as pd
import pytest

from flowcast_eval.metrics import crps_ensemble
from flowcast_eval.pairs import ForecastCube, pairs_from_cube, pairs_from_long
from flowcast_eval.schema import ForecastFormatError, normalize_forecasts, read_forecasts, write_forecasts

LEADS = (6, 12, 24)


def hourly_obs(start="2025-01-01", periods=200, value=100.0):
    idx = pd.date_range(start, periods=periods, freq="h", tz="UTC")
    return pd.Series(value + np.arange(periods, dtype=float), index=idx)


def test_normalize_accepts_archiver_style_columns_and_converts_units():
    raw = pd.DataFrame(
        {
            "source": ["nwm_medium_range_mem1"],
            "location_id": ["01427510"],
            "variable": ["streamflow"],
            "reference_time": ["2025-01-01T00:00Z"],
            "valid_time": ["2025-01-02T00:00Z"],
            "value": [10.0],
            "units": ["m3/s"],
            "ensemble_member": [1],
        }
    )
    df = normalize_forecasts(raw)
    row = df.iloc[0]
    assert row["site_id"] == "USGS-01427510"
    assert row["variable"] == "discharge" and row["unit"] == "ft3/s"
    assert row["value"] == pytest.approx(353.14666721)
    assert row["lead_h"] == 24.0 and row["member"] == 1
    assert row["run_type"] == "operational"


def test_normalize_daily_lead_and_fahrenheit():
    raw = pd.DataFrame(
        {
            "site_id": ["USGS-01427207"],
            "variable": ["water_temperature_daily_max"],
            "model": ["usgs_drb"],
            "issue_time": ["2026-07-01T12:00Z"],
            "valid_time": ["2026-07-03"],
            "value": [77.0],
            "unit": ["degF"],
        }
    )
    row = normalize_forecasts(raw).iloc[0]
    assert row["lead_h"] == 48.0
    assert row["value"] == pytest.approx(25.0)


@pytest.mark.parametrize(
    "change",
    [
        {"variable": "turbidity"},
        {"unit": "furlongs"},
        {"quantile": 1.5},
    ],
)
def test_normalize_rejects_bad_rows(change):
    raw = pd.DataFrame(
        {"site_id": ["USGS-1"], "variable": ["discharge"], "model": ["m"], "issue_time": ["2025-01-01"], "valid_time": ["2025-01-02"], "value": [1.0]}
    )
    for k, v in change.items():
        raw[k] = v
    with pytest.raises(ForecastFormatError):
        normalize_forecasts(raw)


def test_missing_required_column():
    with pytest.raises(ForecastFormatError):
        normalize_forecasts(pd.DataFrame({"site_id": ["USGS-1"]}))


def test_cube_and_long_paths_agree_for_ensembles(tmp_path):
    rng = np.random.default_rng(0)
    issues = pd.date_range("2025-01-02", periods=5, freq="6h", tz="UTC")
    values = 150 + rng.normal(0, 10, (5, len(LEADS), 7))
    cube = ForecastCube("ens", "USGS-1", "discharge", issues, np.array(LEADS, float), values, kind="ensemble")
    obs = hourly_obs()

    write_forecasts(cube.to_long(), tmp_path / "fc.parquet")
    from_long = pairs_from_long(read_forecasts(tmp_path / "fc.parquet"), obs, LEADS).sort_values(["issue_time", "lead_h"]).reset_index(drop=True)
    from_cube = pairs_from_cube(cube, obs).sort_values(["issue_time", "lead_h"]).reset_index(drop=True)

    for col in ["point", "crps", "lo", "hi", "obs", "lead_h"]:
        np.testing.assert_allclose(from_long[col], from_cube[col])
    expected = crps_ensemble(values.reshape(-1, 7), from_cube["obs"].to_numpy())
    np.testing.assert_allclose(np.sort(from_cube["crps"]), np.sort(expected))


def test_long_pairs_bin_irregular_leads():
    fc = normalize_forecasts(
        pd.DataFrame(
            {
                "site_id": "USGS-1",
                "variable": "discharge",
                "model": "marfc",
                "issue_time": ["2025-01-02T13:47Z"] * 3,
                "valid_time": ["2025-01-02T18:00Z", "2025-01-03T00:00Z", "2025-01-03T12:00Z"],
                "value": [1.0, 2.0, 3.0],
            }
        )
    )
    pairs = pairs_from_long(fc, hourly_obs(), LEADS)
    assert pairs["lead_h"].tolist() == [6.0, 12.0, 24.0]
    assert pairs["point"].tolist() == [1.0, 2.0, 3.0]
    assert pairs["obs"].iloc[0] == hourly_obs()[pd.Timestamp("2025-01-02T18:00Z")]
