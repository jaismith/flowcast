# Dam release schedule prototypes

Research prototypes for collecting published dam release schedules, operator-reported releases and pool levels outside the Delaware, and checking them against the gauge below each dam. Nothing here is deployed; the archiver (`archiver/`) is untouched.

Study write-up: `docs/dam-schedule-scraping.md` in the flowcast project store.

## Run

```bash
cd prototypes/dam-schedules
uv sync
uv run flowcast-damsched run --out output/damsched                  # all four sources, ~5 min (polite fetch rate)
uv run flowcast-damsched run --out output/damsched --only swpa      # one source
uv run flowcast-damsched coverage --manifest path/to/manifest.json  # training-basin coverage, ~3 min after first download
uv run pytest
```

`run` writes `records.parquet` (every source in one schema), raw payloads under `raw/`, and `checks.md`. `coverage` writes `coverage.md` and `coverage_dams.csv`. Set `API_DATA_GOV_KEY` for higher USGS rate limits.

## Sources

| Module | Source | Format | What it yields |
|---|---|---|---|
| `sources/swpa.py` | Southwestern Power Administration generation schedules (18 Corps hydro projects, AR/MO/OK/TX) | Fixed-width text in `<PRE>`, one page per weekday | Hourly MW for the day, rolling 7 days |
| `sources/safewaters.py` | Brookfield Renewable Safe Waters (~50 FERC-licensed facilities, mostly ME/NH/NY) | Server-rendered HTML tables plus an embedded JSON object | Interval schedules (cfs) 0–10 days ahead; current flow and pool for every facility |
| `sources/release_calendar.py` | Season-ahead whitewater calendars (PDF, release days as colored cells) | PDF drawing layer | Release days; flow and hours from a small per-calendar spec |
| `sources/lcra.py` | Lower Colorado River Authority Highland Lakes (6 dams, TX) | JSON behind the Hydromet site | Hourly reported dam discharge (14 days), gate-operation notices |

Every source maps to the record shape in `schema.py`: `(source, dam, kind, valid_start, valid_end, value, unit, issue_time, fetched_at, note)`.

## Checks

- `link.py` finds a dam's release gauge from its coordinates: NLDI downstream navigation, then the nearest USGS site with instantaneous discharge in the last 30 days. Many gauges named "below ... Dam" are discontinued, so the name alone isn't enough.
- `validate.py` compares an hourly schedule with the release reference (the dam's CWMS observed outflow where published, else the linked gauge) at the best travel lag. It reports the error of persistence and of persistence plus the scheduled change, which is how a model would use a schedule.
- `coverage.py` flags, for each NID dam of at least 1,000 acre-ft in the training basins, which sources reach it: schedules, observed releases, real-time storage or pool, and history.
