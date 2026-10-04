"""Synoptic issue times: forecasts are issued for 00/06/12/18 UTC only (the training distribution) and run once
their inputs are in, 1.5 h later (production-architecture.md §4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISSUE_HOURS = (0, 6, 12, 18)
INPUT_DELAY = timedelta(hours=1, minutes=30)
# Live forecasts are WY2027 on; nothing is issued (or backfilled) before this (frozen-test hygiene, §8).
FIRST_LIVE_ISSUE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def latest_issue(now: datetime | None = None) -> datetime:
    """The newest synoptic issue whose inputs are available (issue + 1.5 h <= now)."""
    t = (now or utcnow()) - INPUT_DELAY
    hour = max(h for h in ISSUE_HOURS if h <= t.hour)
    return t.replace(hour=hour, minute=0, second=0, microsecond=0)


def issue_key(issue: datetime) -> str:
    return issue.strftime("%Y%m%d%H")


def parse_issue(key: str) -> datetime:
    return datetime.strptime(key, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def check_live(issue: datetime) -> None:
    if issue < FIRST_LIVE_ISSUE:
        raise ValueError(f"issue {issue:%Y-%m-%d %H}Z is before the first live issue {FIRST_LIVE_ISSUE:%Y-%m-%d}; live and backfill runs never read the frozen test years")
    if issue.hour not in ISSUE_HOURS or issue.minute or issue.second:
        raise ValueError(f"{issue} is not a synoptic issue time (00/06/12/18 UTC)")
