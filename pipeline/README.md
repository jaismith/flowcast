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

client = WaterDataClient()  # api.data.gov key from $API_DATA_GOV_KEY (optional; raises rate limits)
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

`sites.yaml` lists forecast sites with their NWS id, NWM reach, NWS stage thresholds and below-dam regulation gauges, plus a `gauges` list: the rest of the Upper Delaware network.

`flowcast-obs` writes hourly observations (the top-of-hour instantaneous value) to `obs/<site>/<variable>/<YYYY-MM>.parquet` in a lake, which is a local directory or `s3://bucket[/prefix]` (`lake.py`):

```bash
uv run flowcast-obs backfill --site 01427510 --start 2000-10-01 --out data          # discharge, water temperature, stage
uv run flowcast-obs ingest --out s3://<lake> --window-days 30   # every gauge in sites.yaml; one request per parameter
uv run flowcast-obs health --out s3://<lake>                    # missed hourly cycles over the last 7 days
```

## Hourly ingest in AWS

The `flowcast-v2` stack (`infra-v2/`) runs `flowcast_pipeline.lambda_handler` as the Lambda `flowcast-obs-ingest`:

- **Hourly** at :10 UTC with a 12-hour trailing window, so a few missed runs heal themselves.
- **Weekly** (Monday 04:40 UTC) with a 30-day window, to pick up USGS revisions.

Each run writes a marker, `_runs/obs-ingest/<date>/<run_id>-<ok|failed>.json`. The skill page reports missed cycles from these markers, and the alarm `flowcast-obs-ingest-missing` fires after 2 hours with no run.

## Snow and radiation module (`flowcast_pipeline.snow`)

SNOW-17 per hydrologic response unit (HRU = sub-basin x elevation band x aspect class) with terrain-corrected shortwave. It produces LSTM input features and mappable per-band / per-sub-basin states. Install with `uv sync --extra snow` (the dev group includes it).

**Model.**

- The Numba kernel is a port of [NOAA-OWP snow17](https://github.com/NOAA-OWP/snow17). With `CLASSIC` parameters it reproduces the OWP Fortran (`tests/test_snow17_reference.py`).
- `NORTHEAST` (the regional default used everywhere) adds three things:
  - **Radiation melt:** melt = temperature factor x (air temperature - MBASE), plus the absorbed terrain-corrected shortwave, with an albedo that decays with snow age.
  - **Wet-bulb split:** a wet-bulb rain/snow ramp.
  - **Humidity/wind rain-on-snow:** the rain-on-snow energy balance uses the forcing vapor pressure and a wind function proportional to wind speed. Classic SNOW-17 assumes 90% relative humidity and a constant wind factor.

**Terrain.**

- Built once per site from AWS Terrain Tiles: slope, aspect, horizon angles (16 directions, 10 km) and sky-view factor.
- Each HRU gets a direct-beam illumination table over sun azimuth and elevation, including shading.
- At run time, global horizontal shortwave is split into beam and diffuse (Erbs). The beam part is scaled by the illumination table averaged over six sun positions per hour, and the diffuse part by the sky-view factor.
- Each band is split into north- and south-facing hillslopes so aspect-driven melt timing is resolved.

```python
from flowcast_pipeline.snow import build_hrus, fetch_nldi_basin, run_snow, basin_features, band_states, map_payload

hrus = build_hrus({"USGS-01423000": fetch_nldi_basin("USGS-01423000")}, n_bands=4, n_aspects=2)
hrus.save("sites/USGS-01423000")        # hrus.parquet, terrain_lut.npz, hrus.geojson (band polygons), meta.json
result = run_snow(hrus, forcing)          # forcing: hourly, canonical names (below)
features = basin_features(result)         # DataFrame[time, LSTM_FEATURES]
states = band_states(result)              # xarray (time, band_id); subbasin_states() for sub-basins
frames = map_payload(result, start="2024-03-01", end="2024-03-04")   # geometry + values[var][time][band]
```

**Forcing** (`FORCING_VARS`). Hourly, UTC. The input is either basin-mean (a DataFrame indexed by time) or per HRU (an xarray Dataset with dims `(time, hru)`).

| Name | Units | Notes |
|---|---|---|
| `precip` | mm per step | Accumulated over the interval ending at the time label |
| `air_temperature` | degC | Gaps up to 72 h are interpolated |
| `specific_humidity` or `dewpoint_temperature` | kg/kg or degC | If missing: 90% relative humidity |
| `surface_pressure` | Pa | If missing: pressure from elevation |
| `wind_speed` or `u_wind`/`v_wind` | m/s | If missing: 3 m/s |
| `shortwave_down` | W/m2 | Flat-surface global horizontal. If missing: 55% of clear-sky |

Basin-mean forcing is lapsed to each band: temperature at -6 degC/km, humidity at constant relative humidity, and hypsometric pressure. `forcing_from_aorc()` maps AORC v1.1 names. AORC shortwave is centered on its time stamp (`radiation_label="center"`).

**Outputs.**

- Per HRU, hourly: `swe`, `rain_plus_melt`, `melt`, `snowfall`, `rainfall`, `snow_cover_frac`, `cold_content`, `liquid_water`, `snow_depth`, `albedo`, `ros_melt`, `rain_on_snow`, `sw_terrain`, `sw_clear_terrain`, `air_temperature`, `wet_bulb`, `snow_frac_precip`.
- `LSTM_FEATURES` (float32):
  - Basin means of the above, prefixed `snow_`, plus `sw_terrain`, `sw_clear_terrain` and `snow_line_elev`.
  - Per-band `snow_swe_b1..4`, `snow_rain_plus_melt_b1..4` and `snow_cover_frac_b1..4`. Band 1 is the lowest; bands are equal-area, so every basin has the same feature width.
- `SnowResult.state` is the end-of-run state for warm starts, e.g. from the hindcast into a forecast.

**Training-dataset step.** Start the forcing at a water-year start (Oct 1) so the model spins up from snow-free conditions. From training cube v1 (`flowcast_pipeline.dataset`), use its AORC elevation-band variables:

```python
from flowcast_pipeline.snow import forcing_from_cube, load_or_build_hrus, snow_features_for_basin
hrus = load_or_build_hrus("USGS-01423000", cache_dir, geometry=camelsh_polygon)   # DEM, bands, forest; cached
forcing, z = forcing_from_cube(cube.sel(basin=basin_id).sel(time=slice("2000-10-01", None)), hrus)
feats = snow_features_for_basin(hrus, forcing, forcing_elevation=z, radiation_label="center", spinup="2001-10-01")
```

`forcing_from_cube` maps each cube band `aorc_band_*` (band, time) onto the HRUs of the same equal-area band. Basin-level wind and pressure go to every HRU, with pressure moved hypsometrically to each band's elevation. A basin-mean DataFrame with AORC names (`APCP_surface`, ...) or cube names (`precip_mm_h`, ...) also works; it is lapsed to the bands. In validation, this basin-mean path scored as well as per-band forcing.

One basin (8 HRUs, 26 years hourly) takes about 1.5 s on one core. The kernel alone runs about 9M HRU-steps/s on 8 cores (`flowcast-snow benchmark`).

**CLI.**

```bash
uv run flowcast-snow build-hrus --site USGS-01423000 --out sites/USGS-01423000
uv run flowcast-snow run --hrus sites/USGS-01423000 --forcing aorc.parquet --out snow_features.parquet --band-states bands.parquet
```

**Validation** (`results/snow_validation/`). The reference is SNODAS basin SWE for 6 Catskills and 5 other Northeast basins, plus GHCN-Daily station SWE in the Catskills; there are no SNOTEL sites in the Northeast. `NORTHEAST` is calibrated on 8 basins over WY2007-2015. Validation covers WY2016-2025 and 3 held-out basins. See `summary.md`, `sensitivity_sweeps.csv` and the plots. Reproduce with `uv run --group snow-validation flowcast-snow-validate fetch|calibrate|sweep|evaluate`.
## Training cube v1 (`flowcast_pipeline.dataset`, `flowcast-dataset`)

Builds the multi-basin hourly training dataset from rebuild plan §5.3: ~550 CAMELSH basins in HUC2 01/02/04/05
(regulated ones included), hourly targets, NID regulation attributes, gauged dam outflows, terrain inputs for the
snow/radiation module, and basin-averaged forcings from AORC, HRRR analysis, MRMS, and archived HRRR and GEFS
forecasts. Output is two Zarr v3 stores per subset, `trainval.zarr` and a physically separate frozen `test.zarr`
(WY2023-2026), in `s3://flowcast-dataset-<account>/v1/{slice50,full}/`.

```bash
uv sync --extra dataset
export FLOWCAST_DATASET_DIR=/data/flowcast-dataset AWS_REGION=...   # same region as the NOAA buckets and GPUs
uv run flowcast-dataset prepare                 # CAMELSH + NID + USGS inventory -> basins, regulation, slice, terrain, weights, plans
uv run flowcast-dataset targets                 # USGS API pulls (1,000 requests/hour; resumable, cached)
uv run flowcast-dataset launch --run r2 --instances 2 --max-minutes 300   # Spot fleet, auto-terminating
uv run flowcast-dataset status --run r2
uv run flowcast-dataset assemble --run r2 --subset slice50 --upload     # early 50-basin slice
uv run flowcast-dataset assemble --run r2 --subset full --upload
```

| Module | What it does |
|---|---|
| `basins.py` | Eligibility (area, record length, still reporting) and the early 50-basin slice |
| `regulation.py` | NID dam attributes, below-dam gauge flags, gauged-outflow discovery (one release gauge per dam) |
| `terrain.py` | Copernicus GLO-90 elevation, slope, aspect, sky view and monthly clear-sky terrain shortwave factors on the AORC grid |
| `grids.py`, `weights.py` | Source grids and exact-ish area weights (supersampled rasterisation), equal-area elevation bands |
| `sources.py` | Product locations, readers and unit conversion to one variable vocabulary |
| `extract.py`, `fleet.py` | Chunk-aligned (block x tile) extraction, two phases (slice tiles first), sharded over Spot instances |
| `targets.py` | Hour-ending mean discharge / water temperature from the USGS client |
| `cube.py` | Zarr assembly: one chunk per basin x whole period, 16 basins per shard, train-split stats, manifest hash |

For training, copy a store to instance NVMe (`aws s3 sync s3://.../v1/full/trainval.zarr /mnt/nvme/trainval.zarr`)
or read it straight from S3; each basin's full series for a variable is one ranged read, so several jobs can
stream the same store concurrently.
