from datetime import datetime, timedelta, timezone

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flowcast_archiver import runner
from flowcast_archiver.model import Issuance, RawPayload
from flowcast_archiver.schema import SCHEMA
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

    monkeypatch.setattr(runner, "SOURCES", {"demo": collect})
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

    monkeypatch.setattr(runner, "SOURCES", {"flaky": flaky})
    store = Store(str(tmp_path))
    report = runner.run(store, now=NOW)
    assert report.failed == ["flaky"]
    assert report.new == {"demo": 1}
    assert store.load_state().has("demo", "a")


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
