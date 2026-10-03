import io

import h5py
import numpy as np
import pandas as pd
import pytest

from flowcast_eval import benchmarks as bm


class FakeResponse:
    def __init__(self, content: bytes, total: int):
        self.content = content
        self.status_code = 206
        self.headers = {"Content-Range": f"bytes 0-{len(content) - 1}/{total}"}

    def raise_for_status(self):
        pass


class FakeSession:
    """Serves byte ranges of an in-memory file and counts the bytes sent."""

    def __init__(self, data: bytes):
        self.data, self.sent = data, 0

    def get(self, url, headers, timeout):
        first, last = (int(x) for x in headers["Range"].removeprefix("bytes=").split("-"))
        chunk = self.data[first : last + 1]
        self.sent += len(chunk)
        return FakeResponse(chunk, len(self.data))


def nwm_like_file(n: int, chunk: int) -> tuple[bytes, np.ndarray]:
    rng = np.random.default_rng(0)
    raw = rng.integers(0, 500_000, n).astype(np.int32)
    raw[7] = -999900
    buf = io.BytesIO()
    with h5py.File(buf, "w") as h:
        h.create_dataset("feature_id", data=np.arange(100, 100 + n, dtype=np.int32), chunks=(chunk,), compression="gzip", shuffle=True)
        ds = h.create_dataset("streamflow", data=raw, chunks=(chunk,), compression="gzip", shuffle=True)
        ds.attrs["scale_factor"] = np.array([0.01], np.float32)
        ds.attrs["_FillValue"] = np.array([-999900], np.int32)
        # padding so the chunks lie beyond the cached head, as in the real files
        h.create_dataset("velocity", data=rng.integers(0, 1 << 30, 4 * n).astype(np.int32))
    return buf.getvalue(), raw


def test_read_reaches_fetches_only_needed_chunks():
    data, raw = nwm_like_file(200_000, 20_000)
    assert len(data) > 2 * bm.HEAD_BYTES
    session = FakeSession(data)
    idx = np.array([7, 5, 45_000, 59_999])
    v = bm.read_reaches("x", idx, session)
    expect = raw[idx] * 0.01
    expect[0] = np.nan
    np.testing.assert_allclose(v, expect, rtol=1e-6)
    assert session.sent < len(data) / 2
    pos = bm.reach_positions("x", [100 + 45_000, 105], FakeSession(data))
    assert list(pos) == [45_000, 5]


def test_read_reaches_by_reach_count():
    data, raw = nwm_like_file(30_000, 10_000)
    v = bm.read_reaches("x", {30_000: np.array([1, 2]), 99: np.array([0])}, FakeSession(data))
    np.testing.assert_allclose(v, raw[[1, 2]] * 0.01, rtol=1e-6)


def test_hads_crosswalk_parses_rows():
    text = "\n".join([
        "NWS  |USGS           |        |   |",
        "-----|---------------|--------|---|-----------|------------|------",
        "CCRN6|01427510       |DD1234AB|BGM|41 45 24   | 75 03 28   |DELAWARE R AT CALLICOON NY",
        "BADXX|notanumber     |        |BGM|0 |0 |x",
    ])
    cw = bm.hads_crosswalk(text)
    assert cw.to_dict("records") == [{"usgs": "01427510", "lid": "CCRN6", "hsa": "BGM"}]


def test_hml_flow_units_and_drops_stage_only():
    df = pd.DataFrame({
        "issued": pd.to_datetime(["2021-01-01 15:00"] * 3, utc=True),
        "valid": pd.to_datetime(["2021-01-01 18:00", "2021-01-02 00:00", "2021-01-02 06:00"], utc=True),
        "primaryname": ["Stage", "Total Discharge", "Pool"],
        "primaryunits": ["ft", "kcfs", "ft"],
        "secondaryname": ["Flow", "Stage", None],
        "secondaryunits": ["kcfs", "ft", None],
        "primary_value": [4.1, 3.5, 1150.0],
        "secondary_value": [2.95, 4.0, np.nan],
    })
    out = bm.hml_flow(df)
    assert out["flow"].tolist() == pytest.approx([2950.0, 3500.0])


def test_pair_bulletins_keeps_off_hour_bulletins_and_interpolates():
    bulletins = pd.DataFrame({
        "issued": pd.to_datetime(["2021-03-01 14:37"] * 3, utc=True),
        "valid": pd.to_datetime(["2021-03-01 18:00", "2021-03-02 00:00", "2021-03-02 06:00"], utc=True),
        "flow": [100.0, 200.0, 400.0],
    })
    T = pd.Timestamp("2021-03-01 12:00", tz="UTC")
    out = bm.pair_bulletins(bulletins, {T: np.array([1, 3, 6, 9, 12, 18, 24])})
    # valid 13:00 and 15:00 are before the bulletin or its trace; 18:00, 21:00, 00:00 and 06:00 are paired
    assert out["valid"].dt.hour.tolist() == [18, 21, 0, 6]
    assert out["flow"].tolist() == pytest.approx([100.0, 150.0, 200.0, 400.0])
    assert (out["issue_time"] == T).all()
    assert out["rfc_lead_h"].iloc[0] == pytest.approx(3 + 23 / 60)
