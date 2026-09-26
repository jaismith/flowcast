from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .config import Config, load_config
from .context import Context
from .http import make_session
from .model import Issuance
from .sources import SOURCES
from .store import Store, utcnow

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_id: str
    new: dict[str, int] = field(default_factory=dict)  # dataset -> new issuances
    rows: dict[str, int] = field(default_factory=dict)  # dataset -> normalized rows
    files: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
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


def run(
    store: Store,
    sources: list[str] | None = None,
    config: Config | None = None,
    now: datetime | None = None,
    backfill_start: datetime | None = None,
) -> RunReport:
    now = now or utcnow()
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    state = store.load_state()
    ctx = Context(config or load_config(), state, make_session(), now, backfill_start)
    report = RunReport(run_id)
    for name in sources or list(SOURCES):
        started = time.monotonic()
        issuances: dict[tuple[str, str], Issuance] = {}
        try:
            for iss in SOURCES[name](ctx):
                issuances.setdefault((iss.dataset, iss.key), iss)
        except Exception:
            log.exception("source %s failed; keeping what it collected", name)
            report.failed.append(name)
        write_issuances(store, run_id, list(issuances.values()), report)
        for iss in issuances.values():
            state.add(iss.dataset, iss.key, iss.issue_time)
        report.seconds[name] = round(time.monotonic() - started, 1)
        log.info("%s: %d new issuances in %.1fs", name, len(issuances), report.seconds[name])
    report.failed += ctx.errors
    state.prune(now)
    store.save_state(state)
    return report
