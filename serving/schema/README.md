# flowcast serving data contract (v1)

What the river page and site selector read. Everything is served by one CloudFront distribution; the page makes no
other data calls (basemap and terrain tiles aside). Schemas are JSON Schema 2020-12; examples are in `../examples/`.

## URLs

| URL | Schema | Cache-Control | Written |
|---|---|---|---|
| `/data/v1/sites.json` | `sites.schema.json` | `max-age=60` | Hourly; lists every site the model can forecast |
| `/data/v1/sites/{id}/live.json` | `live.schema.json` | `max-age=60` | Hourly (observations), after each forecast run |
| `/data/v1/sites/{id}/static.json` | `static.schema.json` | `max-age=300` | At onboarding |
| `/data/v1/sites/{id}/forecasts/{issue}.json` | `forecast.schema.json` | `max-age=31536000, immutable` | Once per forecast run; kept 30 days |
| `/data/v1/gauges/index.json` | `gauges.schema.json#/$defs/index` | `max-age=3600` | Daily 06:15 UTC: rule thresholds, tile list, counts |
| `/data/v1/gauges/ids.json` | `gauges.schema.json#/$defs/ids` | `max-age=3600` | Daily: gauge id -> tile key, for direct links |
| `/data/v1/gauges/tiles/{key}.json` | `gauges.schema.json#/$defs/tile` | `max-age=3600` | Daily: every discharge gauge in a 5° tile with eligibility, `has_q`, `has_temp` |
| `POST /api/visit?site={id}` | `api.schema.json#/$defs/visit` | `no-store` | — |
| `GET /api/status?site={id}` | `api.schema.json#/$defs/status` | `no-store` | — |

- `{id}` is the USGS site id with its agency prefix, e.g. `USGS-01427510`. The APIs also accept a site's `slug`
  (`callicoon`, `lordville`, `allagash`, `accotink`).
- `{issue}` is the forecast's issue time as `YYYYMMDDHH` (UTC), e.g. `2026100412` is 12Z Oct 4, 2026.
- `sites.json` lists all 553 basins the flow model covers. Every one can be woken with `POST /api/visit`;
  `forecast_ready` turns true once its first forecast is published (then `live.json` exists). `has_temp: false` sites
  are flow-only (`has_temp` is the name everywhere: sites.json and static.json). `forecastable: false` (with `not_forecastable_reason`) marks a listed site the rule excludes today, e.g. a
  gauge that stopped reporting discharge; its visit answers `409 not_forecastable`. Other USGS gauges get
  `404 {"error": "not_supported"}` from the APIs for now.
- Page routes: `/site/{slug or id}` serves the single-page app's `index.html` (CloudFront Function); previews live
  under `/preview/{name}/` with the same routing (`/preview/{name}/site/{id}`), reading the production `/data/` files.

Browsers never call USGS: gauge discovery comes from the gauge tiles (key `{floor(lon/5)*5}_{floor(lat/5)*5}`, e.g.
`-80_40`), and a site's readings come from `live.json`, refreshed hourly by the backend for active sites.

A missing file under `/data/` answers 404 (e.g. `live.json` of a site that was never forecast).

## Units and times

- Times are ISO 8601 UTC with a `Z` suffix. Hourly series are hour-ending: the value at `t` covers `(t − 1 h, t]`,
  the USGS and model convention.
- Flow is ft³/s, stage ft, water temperature °C, rain, snow and snowmelt mm, area mi². The page converts for display.
- Regular series are `{start, step_h, values}`: value `i` is at `start + i × step_h` hours. Missing values are `null`.
- Quantile series share one time axis: `q05`, `q25`, `q50`, `q75`, `q95` arrays of equal length.
- In `sites.json`, `state` is the US state of the town in the gauge name (Callicoon and Lordville are NY, although
  USGS files both gauges under PA). The lazy-forecast state is `status` everywhere.

## Page flow

1. Fetch `live.json` (and `static.json` once). Draw observations, then fetch `live.forecast.url` (immutable).
2. Call `POST /api/visit?site={id}` (empty body) on load unless `live.status` is `active` and `live.awake_until` is
   more than 24 h away (always-on sites have `awake_until: null` and never need the call).
3. If the visit response (or `live.status`) is `waking`, poll `GET /api/status?site={id}` every 10 s. When
   `forecast.issue` changes, fetch the new `forecast.url` and swap it in without a reload. Stop polling when
   `status` is no longer `waking` (or after 3 minutes, then show the paused banner).

`status` values (in live, sites, visit and status):

| Status | Meaning | Banner |
|---|---|---|
| `active` | Forecast 4× a day (visited in the last 7 days, popular, or always-on) | None if the forecast is fresh |
| `snoozed` | No recent visit; observations still refresh hourly | None; a visit wakes it |
| `waking` | A wake run is in flight | "Updating the forecast…" |
| `delayed` | Active, but the newest forecast is more than 9 h old | "Forecast delayed. Last updated …" |
| `paused` | The visit-driven cap (50 sites) is reached, or the wake failed | "Showing the forecast from …; live updates are paused." |

A previous forecast is still drawn while waking if `live.forecast.issue_time` is ≤ 3 days old.

## Versioning

`schema` fields carry `flowcast.<kind>/v1`. Additive fields don't bump the version; renames and removals do, and
the old version keeps being written beside the new one until the page has moved.
