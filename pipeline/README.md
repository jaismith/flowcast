# flowcast-pipeline

flowcast v2 data pipeline. So far this package holds the USGS Water Data API client, the site registry, and the local observation lake.

It only talks to `api.waterdata.usgs.gov` (OGC API and STAC). Legacy `waterservices.usgs.gov` is throttled from Nov 16, 2026 and shut down on Feb 22, 2027.

```bash
uv sync
uv run pytest                 # offline tests
uv run pytest -m live         # smoke tests against the real API
```

## USGS client

```python
from flowcast_pipeline.usgs import WaterDataClient, Parameter, Statistic

client = WaterDataClient()  # API key from $API_USGS_PAT (optional; raises rate limits)
q  = client.continuous("01427510", Parameter.DISCHARGE, "2024-01-01", "2024-12-31")          # 15-min, UTC
tw = client.daily("01427510", Parameter.WATER_TEMPERATURE, "2024-06-01", "2024-08-31", Statistic.MAXIMUM)
site = client.monitoring_location("01427510")                   # drainage area, location, HUC, ...
series = client.time_series_metadata("01427510")                 # available parameters and periods
rating = client.rating("01427510")                               # current EXSA stage-discharge rating
rating.stage_to_discharge(9.0)                                   # ~30,600 ft3/s at NWS action stage
latest = client.latest_continuous(["01427510", "01425000"], Parameter.DISCHARGE)
```

| Collection | Method |
|---|---|
| `continuous` (instantaneous values) | `continuous()` |
| `daily` (daily mean/max/min) | `daily()` |
| `latest-continuous` | `latest_continuous()` |
| `monitoring-locations` | `monitoring_location()` |
| `time-series-metadata` | `time_series_metadata()` |
| STAC `ratings` (EXSA/BASE/CORR RDB) | `rating()` |

**Caching.** Responses are cached under `$FLOWCAST_CACHE_DIR/usgs` (default `~/.cache/flowcast/usgs`):

- Continuous values are cached per calendar year and daily values per decade.
- A chunk that is fully in the past and fully `Approved` is kept forever.
- Provisional chunks expire after a day, and the current chunk after 15 minutes, so USGS revisions are picked up.
- Pass `use_cache=False` for small live windows.

**Resilience.**

- Pages are followed through `next` links, up to 50,000 rows per page.
- 429 and 5xx responses are retried with exponential backoff, honoring `Retry-After`.
- An optional `min_interval_s` spaces out requests.

## Site registry and observations

`sites.yaml` lists forecast sites with their NWS id, NWM reach, NWS stage thresholds and below-dam regulation gauges.

`flowcast-obs` writes hourly observations (the top-of-hour instantaneous value) to `obs/<site>/<variable>/<YYYY-MM>.parquet`:

```bash
uv run flowcast-obs backfill --site 01427510 --start 2000-10-01 --out data
uv run flowcast-obs ingest --out data --window-days 30   # re-pull recent data to pick up revisions
```
