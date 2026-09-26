import h5py
import numpy as np
import pytest
import requests

from flowcast_eval.nwm import CFS_PER_CMS, ChunkLayout, read_with_layout


class FakeResponse:
    def __init__(self, content: bytes, status: int = 206):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class RangeSession:
    """Serves byte ranges of a local file, like S3 does."""

    def __init__(self, data: bytes):
        self.data = data
        self.requests = 0

    def get(self, url, headers, timeout):
        self.requests += 1
        start, end = (int(x) for x in headers["Range"].removeprefix("bytes=").split("-"))
        if start >= len(self.data):
            return FakeResponse(b"", 416)
        return FakeResponse(self.data[start : end + 1])


@pytest.fixture
def channel_file(tmp_path):
    rng = np.random.default_rng(0)
    n = 50_000
    values = rng.integers(0, 500_000, n, dtype=np.int32)
    values[123] = -999900
    path = tmp_path / "channel_rt.nc"
    with h5py.File(path, "w") as h:
        h.create_dataset("feature_id", data=np.arange(n, dtype=np.int64) + 1000)
        sf = h.create_dataset("streamflow", data=values, chunks=(n,), shuffle=True, compression="gzip")
        info = sf.id.get_chunk_info(0)
    layout = ChunkLayout(offset=info.byte_offset, n_features=n, index=0, scale=0.01, fill=-999900, size=info.size)
    return path.read_bytes(), values, layout


def test_reads_one_reach_from_shuffled_deflated_chunk(channel_file):
    data, values, layout = channel_file
    for index in (0, 777, len(values) - 1):
        got = read_with_layout("http://x", ChunkLayout(**{**layout.__dict__, "index": index}), RangeSession(data))
        assert got == pytest.approx(values[index] * 0.01 * CFS_PER_CMS)


def test_fill_value_is_nan(channel_file):
    data, _, layout = channel_file
    assert np.isnan(read_with_layout("http://x", ChunkLayout(**{**layout.__dict__, "index": 123}), RangeSession(data)))


def test_undersized_first_range_is_continued(channel_file):
    data, values, layout = channel_file
    session = RangeSession(data)
    small = ChunkLayout(**{**layout.__dict__, "size": 1000, "index": 5})
    assert read_with_layout("http://x", small, session) == pytest.approx(values[5] * 0.01 * CFS_PER_CMS)
    assert session.requests > 1


def test_wrong_layout_returns_none(channel_file):
    data, _, layout = channel_file
    assert read_with_layout("http://x", ChunkLayout(**{**layout.__dict__, "offset": layout.offset + 7}), RangeSession(data)) is None
    assert read_with_layout("http://x", ChunkLayout(**{**layout.__dict__, "n_features": 10}), RangeSession(data)) is None
