"""Hindcast protocol (rebuild plan §8.3).

* Issue times on a fixed UTC cycle (4x/day by default, matching the planned forecast cycle), or taken
  from an opponent's actual issue times so comparisons are like-for-like.
* Only information available at issue time: observations lag by `obs_latency_h` (USGS IV ~1 h).
* Leads are scored on a fixed grid; a forecast at an off-grid lead is assigned to the next grid lead.
* Baselines are fitted on the training years only; the test years are scored once.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pandas as pd

HOURLY_LEADS_H = (1, 2, 3, 6, 9, 12, 18, 24, 30, 36, 42, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 168)
DAILY_LEADS_D = (0, 1, 2, 3, 4, 5, 6, 7)


@dataclass(frozen=True)
class HindcastProtocol:
    name: str
    test_start: str
    test_end: str
    train_start: str = "2000-10-01"
    train_end: str = "2019-09-30T23:00"
    issue_hours_utc: tuple[int, ...] = (0, 6, 12, 18)
    leads_h: tuple[float, ...] = HOURLY_LEADS_H
    obs_latency_h: float = 1.0
    block_days: int = 7
    n_boot: int = 1000
    ci: float = 0.95
    seed: int = 20260926
    notes: tuple[str, ...] = field(default=())

    @property
    def test_window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return pd.Timestamp(self.test_start, tz="UTC"), pd.Timestamp(self.test_end, tz="UTC")

    @property
    def train_window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return pd.Timestamp(self.train_start, tz="UTC"), pd.Timestamp(self.train_end, tz="UTC")

    def issue_times(self, until: pd.Timestamp | None = None) -> pd.DatetimeIndex:
        """Cycle issue times in the test window. `until` (e.g. last obs time) drops issues with no verifying obs."""
        start, end = self.test_window
        if until is not None:
            end = min(end, until)
        hours = pd.date_range(start.floor("D"), end, freq="h")
        return hours[hours.hour.isin(self.issue_hours_utc) & (hours >= start)]

    def with_window(self, test_start: str, test_end: str, name: str | None = None) -> "HindcastProtocol":
        return replace(self, test_start=test_start, test_end=test_end, name=name or self.name)

    def fingerprint(self) -> str:
        """Hash of the protocol and the scoring code, recorded with every scoreboard (plan §8.3 frozen test)."""
        h = hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode())
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            h.update(path.read_bytes())
        return h.hexdigest()[:16]


# Plan §5.3 temporal split: train WY2001-2019, validate WY2020-2022, frozen test WY2023-2026.
FROZEN_TEST = HindcastProtocol(name="frozen-test-wy2023-2026", test_start="2022-10-01", test_end="2026-09-30T23:00")
VALIDATION = HindcastProtocol(name="validation-wy2020-2022", test_start="2019-10-01", test_end="2022-09-30T23:00")
# NWM v3.0 operational output on noaa-nwm-pds starts Jan 2025 (plan milestone 0.4).
NWM_OPERATIONAL = FROZEN_TEST.with_window("2025-01-01", "2026-09-30T23:00", name="nwm-operational-2025+")
# Daily-max water temperature, issued once a day (~8 am EDT) like the USGS Delaware forecast.
TEMPERATURE_DAILY = replace(FROZEN_TEST, name="temperature-daily-max-wy2023-2026", issue_hours_utc=(12,), leads_h=tuple(24.0 * d for d in DAILY_LEADS_D))


def lead_bin(lead_h: pd.Series | float, leads_h: tuple[float, ...]):
    """Smallest grid lead >= lead_h. NaN beyond the grid, and at lead <= 0 unless 0 is a grid lead."""
    grid = pd.Index(sorted(leads_h), dtype=float)
    lead = pd.Series(lead_h, dtype=float) if not isinstance(lead_h, pd.Series) else lead_h.astype(float)
    values = lead.to_numpy()
    pos = grid.searchsorted(values, side="left")
    out = pd.Series(float("nan"), index=lead.index)
    ok = (pos < len(grid)) & ((values > 0) | (values == grid[0]))
    out[ok] = grid[pos[ok]]
    return out
