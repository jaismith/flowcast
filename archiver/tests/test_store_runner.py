import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flowcast_archiver import runner
from flowcast_archiver.model import Issuance, RawPayload
from flowcast_archiver.schema import SCHEMA
from flowcast_archiver.sources import Source
from flowcast_archiver.store import State, Store, read_raw

NOW = datetime(2026, 9, 26, 2, 20, tzinfo=timezone.utc)


def issuance(key: str, issue: datetime, value: float) -> Issuance:
    frame = pd.DataFrame({
        "dataset": "demo", "location_id": "CCRN6", "variable": "flow_cfs",
        "issue_time": [issue], "valid_time": [issue + timedelta(hours=6)], "value": [value], "fetched_at": [NOW],
    })
    raw = RawPayload("https://example.test/x", NOW, 200, "application/json", b'{"ok": true}')
    return Issuance("demo", key, issue, frame, [raw])


@pytest.fixture
def demo_source(monkeypatch):
    calls = []

    def collect(ctx):
        calls.append(ctx)
        for key, issue, value in [("a", NOW - timedelta(hours=1), 1.0), ("b", NOW - timedelta(days=40), 2.0)]:
            if not ctx.state.has("demo", key):
                yield issuance(key, issue, value)

    monkeypatch.setattr(runner, "SOURCES", {"demo": Source(collect)})
    return calls


def test_run_writes_partitioned_files_and_skips_seen_issuances(tmp_path, demo_source):
    store = Store(str(tmp_path))
    report = runner.run(store, now=NOW)
    assert report.ok and report.new == {"demo": 2} and report.rows == {"demo": 2}
    # Issuances are grouped by the month they were issued in.
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")) == [
        "normalized/demo/month=2026-08/20260926T022000Z.parquet",
        "normalized/demo/month=2026-09/20260926T022000Z.parquet",
    ]
    table = pq.read_table(tmp_path / "normalized/demo/month=2026-09/20260926T022000Z.parquet")
    # Parquet has no second-resolution timestamps, so pyarrow stores the schema's seconds as ms.
    assert table.schema.names == SCHEMA.names
    for field in SCHEMA:
        stored = table.schema.field(field.name).type
        if pa.types.is_timestamp(field.type):
            assert pa.types.is_timestamp(stored) and stored.tz == "UTC"
        else:
            assert stored == field.type
    assert table.column("lead_h").to_pylist() == [6.0]
    [record] = read_raw((tmp_path / "raw/demo/month=2026-09/20260926T022000Z.jsonl.zst").read_bytes())
    assert record["key"] == "a" and record["body"] == '{"ok": true}'

    again = runner.run(store, now=NOW + timedelta(hours=1))
    assert again.new == {}


def test_failing_source_keeps_collected_issuances(tmp_path, monkeypatch):
    def flaky(ctx):
        yield issuance("a", NOW, 1.0)
        raise RuntimeError("upstream went away")

    monkeypatch.setattr(runner, "SOURCES", {"flaky": Source(flaky)})
    store = Store(str(tmp_path))
    report = runner.run(store, now=NOW)
    assert report.failed == ["flaky"]
    assert report.new == {"demo": 1}
    assert store.load_state().has("demo", "a")


def test_failed_write_leaves_issuances_for_the_next_run(tmp_path, monkeypatch):
    def source(ctx):
        ctx.state.advance_cursor("demo", NOW)
        yield issuance("a", NOW, 1.0)

    def broken_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "SOURCES", {
        "demo_source": Source(source), "other": Source(lambda ctx: iter([issuance("b", NOW, 2.0)])),
    })
    store = Store(str(tmp_path))
    monkeypatch.setattr(store, "write_normalized", broken_write)
    report = runner.run(store, now=NOW)
    assert report.failed == ["demo_source (write)", "other (write)"]
    state = store.load_state()
    assert not state.has("demo", "a") and state.cursor("demo") is None


def test_files_use_codecs_the_lambda_layer_supports(tmp_path):
    store = Store(str(tmp_path))
    key = store.write_normalized("demo", "2026-09", "run", issuance("a", NOW, 1.0).frame)
    meta = pq.ParquetFile(tmp_path / key).metadata
    assert {meta.row_group(0).column(i).compression for i in range(meta.num_columns)} == {"SNAPPY"}
    raw_key = store.write_raw("demo", "2026-09", "run", [{"key": "a"}])
    assert (tmp_path / raw_key).read_bytes()[:4] == b"\x28\xb5\x2f\xfd"  # zstd frame magic
    assert read_raw((tmp_path / raw_key).read_bytes()) == [{"key": "a"}]


def test_read_raw_accepts_streamed_frames_without_content_size():
    sink = pa.BufferOutputStream()
    with pa.CompressedOutputStream(sink, "zstd") as out:
        out.write(b'{"key": "old"}\n')
    assert read_raw(sink.getvalue().to_pybytes()) == [{"key": "old"}]


def test_state_prune_keeps_backfill_overlap():
    state = State()
    state.add("demo", "old", NOW - timedelta(days=60))
    state.add("demo", "recent", NOW - timedelta(days=1))
    state.add("rvf", "walking", datetime(2003, 6, 1, tzinfo=timezone.utc))
    state.prune(NOW)
    assert not state.has("demo", "old") and state.has("demo", "recent")
    # A backfill cursor years in the past keeps its own recent keys.
    assert state.has("rvf", "walking")
    assert State.from_json(state.to_json()).cursors == state.cursors


def test_unsupported_store_uri():
    with pytest.raises(ValueError):
        Store("gs://bucket/prefix")


def test_slow_and_one_time_sources_skip_runs_until_due(tmp_path, monkeypatch):
    calls = []

    def make(name):
        def collect(ctx):
            calls.append(name)
            return iter([issuance(f"{name}-{ctx.now.isoformat()}", ctx.now, 1.0)])
        return collect

    monkeypatch.setattr(runner, "SOURCES", {
        "hourly": Source(make("hourly")),
        "six_hourly": Source(make("six_hourly"), every=timedelta(hours=6)),
        "once": Source(make("once"), once=True),
    })
    store = Store(str(tmp_path))
    for hour in range(8):
        runner.run(store, now=NOW + timedelta(hours=hour, minutes=hour))  # runs drift a little later each hour
    assert calls.count("hourly") == 8
    assert calls.count("six_hourly") == 2  # hours 0 and 6
    assert calls.count("once") == 1
    runner.run(store, now=NOW + timedelta(hours=8), force=True)
    assert calls.count("once") == 2


def test_sources_skip_when_time_budget_is_short(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "SOURCES", {"demo": Source(lambda ctx: iter([issuance("a", NOW, 1.0)]))})
    store = Store(str(tmp_path))
    report = runner.run(store, now=NOW, deadline=time.monotonic() + 10)
    assert report.skipped == {"demo": "time budget"} and report.new == {}
    # Still due next time, since it never ran.
    assert runner.run(store, now=NOW + timedelta(hours=1)).new == {"demo": 1}


def test_failed_source_stays_due(tmp_path, monkeypatch):
    def broken(ctx):
        raise RuntimeError("down")
        yield

    monkeypatch.setattr(runner, "SOURCES", {"slow": Source(broken, every=timedelta(hours=6))})
    store = Store(str(tmp_path))
    runner.run(store, now=NOW)
    assert store.load_state().last_run_at("slow") is None
    assert "slow" not in runner.run(store, now=NOW + timedelta(hours=1)).skipped
