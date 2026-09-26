from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from .config import Config, load_config
from .context import Context
from .http import make_session
from .model import Issuance
from .sources import SOURCES, Source
from .store import State, Store, utcnow

log = logging.getLogger(__name__)

# A 6-hourly source scheduled at :20 stays due even if the previous run finished a few minutes late.
SCHEDULE_SLACK = timedelta(minutes=15)
# Don't start a source with less than this much of the invocation left.
MIN_SOURCE_BUDGET_S = 90.0


@dataclass
class RunReport:
    run_id: str
    new: dict[str, int] = field(default_factory=dict)  # dataset -> new issuances
    rows: dict[str, int] = field(default_factory=dict)  # dataset -> normalized rows
    files: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # source -> reason
    seconds: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def _month(ts: datetime) -> str:
    return ts.strftime("%Y-%m")


def write_issuances(store: Store, run_id: str, issuances: list[Issuance], report: RunReport) -> None:
    groups: dict[tuple[str, str], list[Issuance]] = defaultdict(list)
    for iss in issuances:
        groups[(iss.dataset, _month(iss.issue_time))].append(iss)
    for (dataset, month), items in sorted(groups.items()):
        frames = [i.frame for i in items if i.frame is not None and not i.frame.empty]
        if frames:
            frame = pd.concat(frames, ignore_index=True)
            report.files.append(store.write_normalized(dataset, month, run_id, frame))
            report.rows[dataset] = report.rows.get(dataset, 0) + len(frame)
        records = [p.to_record(dataset, i.key) for i in items for p in i.raw]
        if records:
            report.files.append(store.write_raw(dataset, month, run_id, records))
        report.new[dataset] = report.new.get(dataset, 0) + len(items)


def is_due(name: str, source: Source, state: State, now: datetime) -> bool:
    last = state.last_run_at(name)
    if last is None:
        return True
    if source.once:
        return False
    return source.every is None or now - last >= source.every - SCHEDULE_SLACK


def run(
    store: Store,
    sources: list[str] | None = None,
    config: Config | None = None,
    now: datetime | None = None,
    backfill_start: datetime | None = None,
    force: bool = False,
    deadline: float | None = None,
) -> RunReport:
    """One archive pass. `deadline` is a time.monotonic() value; sources not yet started when it
    gets close are skipped (and stay due for the next run). `force` ignores the schedules."""
    now = now or utcnow()
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    state = store.load_state()
    ctx = Context(config or load_config(), state, make_session(), now, backfill_start)
    report = RunReport(run_id)
    for name in sources or list(SOURCES):
        source = SOURCES[name]
        if not force and not is_due(name, source, state, now):
            report.skipped[name] = "not due"
            continue
        if deadline is not None and deadline - time.monotonic() < MIN_SOURCE_BUDGET_S:
            report.skipped[name] = "time budget"
            log.warning("skipping %s: too little time left in this run", name)
            continue
        started = time.monotonic()
        cursors_before = dict(state.cursors)
        issuances: dict[tuple[str, str], Issuance] = {}
        completed = True
        try:
            for iss in source.collect(ctx):
                issuances.setdefault((iss.dataset, iss.key), iss)
        except Exception:
            log.exception("source %s failed; keeping what it collected", name)
            report.failed.append(name)
            completed = False
        try:
            write_issuances(store, run_id, list(issuances.values()), report)
        except Exception:
            # Leave these issuances unseen (and cursors where they were) so the next run retries them.
            log.exception("writing %s failed", name)
            report.failed.append(f"{name} (write)")
            state.cursors = cursors_before
            issuances = {}
            completed = False
        for iss in issuances.values():
            state.add(iss.dataset, iss.key, iss.issue_time)
        if completed:
            state.last_run[name] = now.isoformat()
        # Saved after every source so a timeout later in the run loses nothing already written.
        store.save_state(state)
        report.seconds[name] = round(time.monotonic() - started, 1)
        log.info("%s: %d new issuances in %.1fs", name, len(issuances), report.seconds[name])
    report.failed += ctx.errors
    report.warnings += ctx.warnings
    state.prune(now)
    store.save_state(state)
    return report
