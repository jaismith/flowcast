from datetime import datetime, timezone

import pandas as pd

from flowcast_archiver.sources import iem
from conftest import FETCHED_AT, fixture_text


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def as_afos(*products: str) -> str:
    """Frame products the way the IEM text endpoint serves them: SOH/ETX and CR CR LF line ends."""
    return "".join("\x01\r\r\n" + p.replace("\n", "\r\r\n") + "\x03" for p in products)


def test_split_products_strips_framing_and_carriage_returns():
    a, b = fixture_text("rvf_RVFNY2_2000.txt"), fixture_text("rvf_RVFUDE_2023.txt")
    products = iem.split_products(as_afos(a, b))
    assert len(products) == 2
    assert all("\r" not in p for p in products)
    assert products[0].startswith("FGUS51 KRHA 311415")


def test_parse_rvfny2_skips_column_heading_comments():
    [product] = iem.split_products(as_afos(fixture_text("rvf_RVFNY2_2000.txt")))
    messages = {m.lid: m for m in iem.parse_e_messages(product)}
    ccrn = messages["CCRN6"]
    assert ccrn.pe == "HG"
    # "915 AM EST SUN DEC 31 2000"
    assert ccrn.created == utc(2000, 12, 31, 14, 15)
    # DH13 EST is 18Z; the commented-out 7AM slot is skipped; E2 rolls over to Jan 1.
    assert ccrn.values == [
        (utc(2000, 12, 31, 18), 3.4),
        (utc(2001, 1, 1, 0), 3.4),
        (utc(2001, 1, 1, 6), 3.4),
        (utc(2001, 1, 1, 12), 3.4),
    ]
    assert len(messages["BRYN6"].values) == 8


def test_parse_rvfude_reservoir_pool_in_utc():
    [product] = iem.split_products(as_afos(fixture_text("rvf_RVFUDE_2023.txt")))
    messages = {m.lid: m for m in iem.parse_e_messages(product)}
    pep = messages["PEPN6"]
    assert pep.pe == "HP"
    assert pep.created == utc(2023, 1, 4, 15, 37)
    assert len(pep.values) == 12
    assert pep.values[0] == (utc(2023, 1, 4, 18), 1270.4)
    assert pep.values[-1] == (utc(2023, 1, 7, 12), 1272.1)
    assert messages["HVDN6"].values[:3] == [(utc(2023, 1, 4, 18), 3.8), (utc(2023, 1, 5, 0), 3.7), (utc(2023, 1, 5, 6), 3.9)]


def test_resolve_date_handles_leap_day_and_year_rollover():
    assert iem._resolve_date("0229", datetime(2000, 3, 1)) == (2000, 2, 29)
    # Only 2000 of 1999-2001 has a Feb 29, so it wins even from a 2001 reference.
    assert iem._resolve_date("0229", datetime(2001, 1, 2)) == (2000, 2, 29)
    assert iem._resolve_date("0101", datetime(2000, 12, 31, 14)) == (2001, 1, 1)
    assert iem._resolve_date("231231", datetime(2024, 1, 1)) == (2023, 12, 31)
    assert iem._resolve_date("20240105", datetime(2024, 1, 1)) == (2024, 1, 5)


def test_malformed_message_is_skipped_not_fatal():
    product = "\n".join([
        ".E CCRN6 1231 E DC0013310915/DH13/HGIFF/DIH6",  # month 13
        ".E1 :1231: :       :/      3.4/      3.4/      3.4",
        ".E BRYN6 1231 E DC0012310915/DH13/HGIFF/DIH6",
        ":QPF FCST        7AM       1PM       7PM       1AM",
        ".E1 :1231: :       :/      3.6/      M/      3.5",
    ])
    [msg] = iem.parse_e_messages(product)
    assert msg.lid == "BRYN6"
    # "M" is missing: dropped, but the time step still advances past it.
    assert msg.values == [(utc(2000, 12, 31, 18), 3.6), (utc(2001, 1, 1, 6), 3.5)]


def test_normalize_product_adds_rated_flow_for_stage_points(ctx):
    [product] = iem.split_products(as_afos(fixture_text("rvf_RVFNY2_2000.txt")))
    issue_time, df = iem.normalize_product(product, "RVFNY2", ctx, FETCHED_AT)
    assert issue_time == utc(2000, 12, 31, 14, 15)
    assert set(df["dataset"]) == {"marfc_rvf"}
    ccrn = df[df["location_id"] == "CCRN6"]
    stage = ccrn[ccrn["variable"] == "stage_ft"]
    flow = ccrn[ccrn["variable"] == "flow_cfs"]
    assert (stage["qualifier"] == "RVFNY2").all()
    assert (flow["qualifier"] == "usgs_rating:01427510:17.0").all()
    assert (ccrn["usgs_site"] == "01427510").all()
    assert len(flow) == len(stage) == 4
    assert flow["value"].iloc[0] == pd.Series([1319.53]).iloc[0]
    # No rating is cached for other gauges and the network is off: stage only, no failure.
    assert set(df[df["location_id"] == "BRYN6"]["variable"]) == {"stage_ft"}


def test_normalize_product_keeps_pool_elevation_without_rating(ctx):
    [product] = iem.split_products(as_afos(fixture_text("rvf_RVFUDE_2023.txt")))
    _, df = iem.normalize_product(product, "RVFUDE", ctx, FETCHED_AT)
    pep = df[df["location_id"] == "PEPN6"]
    assert set(pep["variable"]) == {"pool_elev_ft"}
    assert pep["usgs_site"].isna().all()
