"""Runtime settings, from the Lambda environment (infra-v2/lib/serving.ts) or a developer's shell."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    lake_uri: str = field(default_factory=lambda: os.environ.get("LAKE_URI", ""))
    # Bucket holding the public JSON under `data/` (served at /data/* by CloudFront).
    data_bucket: str = field(default_factory=lambda: os.environ.get("DATA_BUCKET", ""))
    # Optional key prefix inside the data bucket for test runs (e.g. "dev/jai/"); "" in production.
    data_prefix: str = field(default_factory=lambda: os.environ.get("DATA_PREFIX", ""))
    table: str = field(default_factory=lambda: os.environ.get("CONTROL_TABLE", "flowcast-control"))
    forecast_function: str = field(default_factory=lambda: os.environ.get("FORECAST_FUNCTION", "flowcast-forecast"))
    event_bus: str = field(default_factory=lambda: os.environ.get("EVENT_BUS", "default"))
    work_dir: str = field(default_factory=lambda: os.environ.get("FLOWCAST_WORK_DIR", "/tmp/flowcast"))
    metrics_namespace: str = "flowcast/serving"


# Lazy-forecasting settings (production-architecture.md §5, decided Oct 4, 2026).
SNOOZE_DAYS = 7
POPULAR_DAYS = 30
POPULAR_MIN_VISIT_DAYS = 3
POPULAR_WINDOW_DAYS = 14
VISIT_ACTIVE_CAP = 50
# A wake or cycle run holds its lock this long; a crashed run is retried after it expires.
RUN_LEASE_MIN = 15
MAX_ATTEMPTS = 3
# Forecasts older than this are "delayed" for an active site.
DELAYED_AFTER_H = 9
# Typical wake time reported to the page before a wake has been measured.
WAKE_ETA_S = 60
