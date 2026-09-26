"""Parsers for the dam release-schedule sources (ODRM, NYC DEP, NYSDEC thermal, CWMS, TVA)."""

from collections import Counter
from datetime import date, datetime, timezone

import pandas as pd

from flowcast_archiver.sources import cwms, nyc_dep, odrm, thermal, tva
from conftest import FETCHED_AT, FIXTURES, fixture_json, fixture_text


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


# ODRM


def test_odrm_lists_30_day_zips_from_data_page():
    names = odrm.series_names(fixture_text("odrm_data_page.html"))
    assert names and all("7day" not in n and "/" not in n for n in names)
    assert names == sorted(set(names))


def test_odrm_target_flow_zip():
    text = odrm.read_zip((FIXTURES / "odrm_cannonsville_target_flow.zip").read_bytes())
    df = odrm.parse_csv(text, "Cannonsville Target Flow", FETCHED_AT)
    assert len(df) == 30
    assert set(df["location_id"]) == {"cannonsville"}
    assert set(df["variable"]) == {"target_flow_cfs"}
    first = df.iloc[0]
    assert first["valid_time"] == ts("2026-08-26T04:00:00")  # local midnight, EDT
    assert first["value"] == 549.0
    assert (df["issue_time"] == FETCHED_AT).all() and set(df["qualifier"]) == {"Cannonsville Target Flow"}


def test_odrm_basin_series_and_units():
    text = odrm.read_zip((FIXTURES / "odrm_flow_target.zip").read_bytes())
    df = odrm.parse_csv(text, "Flow Target", FETCHED_AT)
    assert set(df["location_id"]) == {"odrm"} and set(df["variable"]) == {"flow_target_cfs"}
    # The Montague flow target runs a few days past the fetch.
    assert df["valid_time"].max() > pd.Timestamp(FETCHED_AT)
    assert odrm.location_and_variable("Montague Discharge", "Discharge ft^3/s") == ("montague", "discharge_cfs")
    assert odrm.location_and_variable("Pepacton Storage", "Reservoir storage M US Gal") == ("pepacton", "storage_mg")
    assert odrm.location_and_variable("Neversink Diversion", "Discharge M US Gal/d") == ("neversink", "diversion_mgd")
    assert odrm.location_and_variable("Thermal Mitigation Bank", "Reservoir storage ft^3/s-day") == (
        "odrm", "thermal_mitigation_bank_cfs_day")


def test_odrm_header_only_csv_is_empty():
    assert odrm.parse_csv("Date,Reservoir storage M US Gal\r\n", "New Jersey Diversion Amelioration Bank", FETCHED_AT).empty


# NYC DEP


def test_dep_release_page():
    as_of, df = nyc_dep.parse_release_page(fixture_text("nyc_dep_release_levels.html"), FETCHED_AT)
    assert as_of == "2026-09-25"
    values = df.set_index(["location_id", "variable"])["value"]
    assert values[("cannonsville", "release_mgd")] == 275
    assert values[("pepacton", "release_mgd")] == 94
    assert values[("neversink", "release_mgd")] == 65
    assert values[("ashokan", "turbidity_ntu")] == 5.5
    assert (df["valid_time"] == ts("2026-09-25T04:00:00")).all()


def test_opendata_maps_columns_by_scada_tag():
    fx = fixture_json("nyc_opendata_reservoirs.json")
    df = nyc_dep.normalize_opendata(fx["records"], fx["columns"], FETCHED_AT)
    assert len(df) == 3 * len(nyc_dep.TAGS)
    day1 = df[df["valid_time"] == ts("2017-11-01T12:00:00")].set_index(["location_id", "variable"])
    # Field names are shifted for the Delaware reservoirs: `cannonsville_release` is Pepacton storage (EDIRESVOLUME).
    assert day1.loc[("pepacton", "storage_bg"), "value"] == 116.6
    assert day1.loc[("pepacton", "storage_bg"), "qualifier"] == "EDIRESVOLUME"
    assert day1.loc[("pepacton", "elevation_ft"), "value"] == 1264.45
    assert day1.loc[("cannonsville", "release_mgd"), "value"] == 97.3
    assert day1.loc[("neversink", "storage_bg"), "value"] == 30.74
    assert (df["issue_time"] == df["valid_time"]).all()


# NYSDEC thermal requests


def test_thermal_index_assigns_years():
    requests_ = thermal.list_requests(fixture_text("ffmp_index_thermal.html"), date(2026, 9, 26))
    assert len(requests_) == 97
    assert Counter(r.day.year for r in requests_) == {
        2019: 9, 2020: 22, 2021: 7, 2022: 25, 2023: 3, 2024: 11, 2025: 10, 2026: 10}
    by_slug = {r.slug: r.day for r in requests_}
    for slug, day in by_slug.items():
        if slug[-4:].isdigit():
            assert day.year == int(slug[-4:]), slug
    # No year in the slug: inferred from the index order.
    assert by_slug["thermal-release-request-2"] == date(2023, 9, 5)
    assert by_slug["thermal-release-request-1"] == date(2020, 7, 28)


def _schedule(name: str, day: date) -> pd.DataFrame:
    df = thermal.parse_request(fixture_text(name), day)
    return df.pivot_table(index=["location_id", "valid_time"], columns="variable", values="value")


def test_thermal_request_with_absolute_flows():
    s = _schedule("thermal_july-29-2025.html", date(2025, 7, 29)).loc["Cannonsville"]
    assert s.loc[ts("2025-07-30T00:00:00"), "release_before_request_cfs"] == 500  # 8 PM EDT
    assert list(s["requested_release_cfs"]) == [600, 750, 900, 750, 600, 500]
    assert s.index[-1] == ts("2025-07-30T14:00:00")
    assert list(s["requested_change_cfs"]) == [100, 150, 150, -150, -150, -100]


def test_thermal_request_with_changes_only_uses_stated_base():
    s = _schedule("thermal_july-5-2023.html", date(2023, 7, 5)).loc["Cannonsville"]
    assert s["release_before_request_cfs"].dropna().tolist() == [500]  # "... L2 release of 500 cfs"
    assert list(s["requested_release_cfs"]) == [625, 750, 625, 500]


def test_thermal_request_for_three_reservoirs_over_two_days():
    s = _schedule("thermal_july-19-2019.html", date(2019, 7, 19))
    assert set(s.index.get_level_values(0)) == {"Cannonsville", "Pepacton", "Neversink"}
    assert s.loc[("Pepacton", ts("2019-07-19T20:00:00")), "requested_release_cfs"] == 175
    assert s.loc[("Cannonsville", ts("2019-07-21T16:01:00")), "requested_release_cfs"] == 750  # 12:01 PM Sunday


def test_thermal_table_running_past_midnight():
    s = _schedule("thermal_august-10-2021.html", date(2021, 8, 10)).loc["Cannonsville"]
    assert s.index.tolist() == [ts("2021-08-10T20:00:00"), ts("2021-08-11T08:00:00")]
    assert list(s["requested_release_cfs"]) == [675, 600]


# USACE CWMS


def test_cwms_selects_active_operator_outflow_forecasts():
    entries = fixture_json("cwms_catalog.json")["entries"]
    now = datetime(2026, 9, 26, 4, tzinfo=timezone.utc)
    names = [e["name"] for e in cwms.select_series(entries, now)]
    assert names == [
        "Barren.Flow-Outflow.Inst.1Hour.0.National-CWMS-Forecast",
        "Brookville.Flow-Outflow.Inst.1Hour.0.National-CWMS-Forecast",
        "ALF.Flow-Out.Ave.~1Day.1Day.CBT-Proj-FCST-REV",
    ]  # not RFC-sourced, not inflow, not an observed ("Best") version
    assert cwms.select_series(entries, datetime(2026, 12, 1, tzinfo=timezone.utc)) == []


def test_cwms_values_in_cfs():
    body = fixture_json("cwms_timeseries.json")
    df = cwms.normalize_values(body, "LRL", FETCHED_AT)
    assert len(df) == 12
    assert set(df["location_id"]) == {"LRL/Barren"} and set(df["variable"]) == {"outflow_cfs"}
    assert df["valid_time"].iloc[0] == pd.Timestamp(body["values"][0][0], unit="ms", tz="UTC")
    metric = dict(body, units="cms")
    assert abs(cwms.normalize_values(metric, "LRL", FETCHED_AT)["value"].iloc[0] - df["value"].iloc[0] * 35.3146667) < 1e-6


# TVA


def test_tva_predicted_data():
    df = tva.normalize(fixture_json("tva_predicted_DUGT1.json"), "DUGT1", FETCHED_AT)
    assert len(df) == 9
    v = df.set_index(["variable", "valid_time"])["value"]
    assert v[("outflow_cfs", ts("2026-09-25T04:00:00"))] == 5292  # "5,292"
    assert v[("inflow_cfs", ts("2026-09-25T04:00:00"))] == 3117
    assert abs(v[("pool_elev_ft", ts("2026-09-26T04:00:00"))] - 978.55) < 0.01  # midnight ending the day
    assert set(df.loc[df["variable"] == "pool_elev_ft", "qualifier"]) == {"midnight"}
