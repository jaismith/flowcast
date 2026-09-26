# flowcast-eval

The evaluation harness for flowcast v2. It covers the hindcast protocol, metrics, bootstrap confidence intervals, reference baselines, NWM baselines and scoreboards (rebuild plan §8 and milestones 0.4–0.6).

```bash
uv sync
uv run pytest
uv run flowcast-eval fetch-nwm --site 01427510                                         # cache NWM retrospective + operational archive
uv run flowcast-eval scoreboard --site 01427510 --out results/USGS-01427510            # baselines + NWM + temperature
uv run flowcast-eval score --forecasts <parquet or dir> --site 01427510 --out results/x # score archived forecasts
```

`results/USGS-01427510/scoreboard.md` holds the current Callicoon baseline scoreboard.

## Forecast input format

Every forecast source writes long-format Parquet, one row per (site, variable, model, issue time, valid time, member or quantile). This includes flowcast models, the NWS/HEFS/NWM archiver and baselines. The full contract is in `src/flowcast_eval/schema.py`.

| column | required | notes |
|---|---|---|
| `site_id` | yes | `USGS-01427510` (a bare `01427510` is accepted) |
| `variable` | yes | `discharge`, `stage`, `water_temperature`, `water_temperature_daily_max`, `water_temperature_daily_mean` |
| `model` | yes | e.g. `marfc`, `hefs`, `nwm_medium_range_mem1`, `usgs_drb_temperature` |
| `issue_time` | yes | UTC; when the forecast was issued (or its nominal cycle time) |
| `valid_time` | yes | UTC; for daily variables, midnight UTC of the site-local date |
| `value` | yes | numeric |
| `unit` | no | defaults to `ft3/s`, `ft` or `degC`. `m3/s`, `kcfs`, `cfs`, `m` and `degF` are converted |
| `lead_h` | no | derived when absent |
| `member` | no | ensemble member (int). Set at most one of `member` and `quantile` |
| `quantile` | no | quantile level in (0, 1) |
| `run_type` | no | `operational` (default), `perfect_forcing` or `simulation` |
| `qualifier` | no | free text, e.g. a HEFS trace year or bulletin id |

Common alternative names are accepted as aliases:

- `source` for `model`;
- `location_id` / `usgs_id` for `site_id`;
- `reference_time` for `issue_time`;
- `lead_hours` for `lead_h`;
- `ensemble_member` / `trace` for `member`;
- `units` for `unit`;
- `flow` / `streamflow` for `discharge`.

Hive-partitioned directories work too.

## Protocol (`protocol.py`)

- **Splits** (plan §5.3): fit on WY2001–2019, validate on WY2020–2022. WY2023–2026 is the frozen test.
- **Issue times:** 00/06/12/18 UTC. When comparing against an opponent, use that opponent's own issue times; `score` does this automatically.
- **Latency:** observations lag issue time by 1 h, the USGS instantaneous-value latency.
- **Leads:** scored on a grid from 1 h to 168 h. Off-grid leads move to the next grid lead. Daily variables are scored at days 0–7.
- **Alignment:** only (issue time, lead) pairs that every competing model forecasts are scored.
- **Fingerprint:** every scoreboard records a hash of the protocol plus the scoring code, for the frozen test.

## Metrics (`metrics.py`, `scoring.py`)

- **Metrics:** NSE, KGE (Gupta 2009) with its r/α/β components, MAE, RMSE, bias, percent bias and 80% interval coverage.
- **CRPS:** fair ensemble CRPS for ensembles, the pinball-integral approximation for quantiles, and absolute error for deterministic forecasts.
- **Aggregation:** everything is computed from additive per-day sums, so resampling is cheap.
- **Bootstrap:** paired moving-block bootstrap over issue days (7-day blocks, 1,000 draws), with 95% intervals for each score. Model-minus-reference differences and skill scores get intervals too, flagged ▲/▼ when significant.
- **Breakdowns:** by season, by flow regime (below Q25, mid, above Q90 of the training years), and above NWS action stage.

## Baselines (`baselines/`, `nwm.py`)

| model | what |
|---|---|
| `persistence` | Last observation available at issue time |
| `recession_persistence` | Persistence, except on a falling limb, where a fitted master recession curve `-dQ/dt = aQ^b` takes over |
| `climatology` | Day-of-year (±7 days) quantiles from the training years (50 quantiles) |
| `air2stream_obs_air` / `air2stream_clim_air` | air2stream (8-parameter) on daily maxima, driven by ERA5 air temperature (perfect forcing) or by air-temperature climatology (operationally fair) |
| `nwm_retrospective` | NWM v3.0 retrospective: an AORC-forced simulation with no data assimilation, read from the public Zarr store |
| `nwm_short_range`, `nwm_medium_range_mem1`, `nwm_medium_range_blend`, `nwm_medium_range_ensemble` | Archived NWM v3 operational forecasts from `noaa-nwm-pds` (Jan 2025+) |

There is no public NWM reforecast, so the archived operational forecasts stand in for one. Each forecast file is a 12–15 MB CONUS NetCDF. The reader fetches only the compressed streamflow chunk (about 0.6–2 MB) with HTTP range requests, then un-shuffles and inflates it. h5py is used only to rediscover the byte layout when the fast path's size check fails.
