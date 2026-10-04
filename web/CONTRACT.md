# What the page reads from the serving backend

Against the v1 contract in `serving/schema/` (PR #64), as deployed. Types: `src/lib/contract.ts`; checks and
normalization: `src/lib/site.ts`. Optional fields may be null for a site; the page drops the part noted.
A missing file (404, or 403 from a bare S3 origin) means not published yet.

## `/data/v1/sites.json`

| Field | Used for |
|---|---|
| `schema` | must be `flowcast.sites/v1` |
| `sites[].id`, `slug` | routes: `/site/USGS-…` is canonical; `/site/<slug>` resolves to it |
| `sites[].name`, `river`, `town`, `state` | page title, header (`<river> at <town>, <state>`), not-found and not-served pages |
| `sites[].lat`, `lon`, `area_mi2` | fallbacks when static.json is missing; the site map and "nearest" in the selector |
| `sites[].forecastable`, `not_forecastable_reason` | false: the page shows "can't forecast" with the reason's text from the gauge catalog's `rule.reasons`, and sends no visit |
| `sites[].forecast_ready`, `has_temp` | the map's dot (forecast ready, starts when opened, flow only) |
| `sites[].status`, `always_on` | whether to POST a visit before live.json is read |
| `default` | the site at `/` when this browser hasn't opened one before |

## `/data/v1/gauges/` (the national gauge catalog)

| File | Used for |
|---|---|
| `index.json`: `tile_deg`, `tile_url`, `tiles` | which tiles to load for the map's view (from zoom 6) |
| `index.json`: `rule.reasons` | the text for reason codes ("discharge (00060) not reported in the last 7 days") |
| `tiles/{key}.json`: `gauges[].id`, `name`, `lat`, `lon`, `area_km2` | map dots, search rows (the USGS name is parsed into river, town and state) |
| `tiles/{key}.json`: `gauges[].eligibility` (`status`, `forecast_now`, `reasons`), `has_temp` | dot style; the "can't forecast" page for a gauge outside `sites.json` |
| `ids.json`: `tiles` | a direct link to a gauge whose tile the map hasn't loaded (about 350 KB, fetched only then) |

## `/data/v1/sites/{id}/live.json`

| Field | Used for |
|---|---|
| `schema`, `id` | must be `flowcast.live/v1` and match the route |
| `status`, `always_on`, `awake_until` | whether to POST a visit; the banners |
| `forecast` (`issue`, `issue_time`, `url`) | the forecast to fetch; whether to keep drawing it while waking (≤ 3 days old) |
| `static_url` | static.json |
| `now.observed_at`, `flow_cfs`, `stage_ft`, `water_temp_c`, `gauge_stale` | the Now slot, river level and flood bar, water temperature, "Updated …" |
| `now.flow_change_24h_cfs`, `flood_category` | the warming-up page's Now trend and river-level label (before a forecast is drawn) |
| `observations.discharge` (hourly) | the past 3 days on the flow chart; 24 h trend; "Now" at the forecast's issue |
| `observations.water_temperature` (hourly) | the past 3 days on the temperature chart |

## `/data/v1/sites/{id}/static.json`

| Field | Used for |
|---|---|
| `name`, `short_name`, `river`, `lat`, `lon`, `nws_lid`, `usgs_url` | header, headings, About |
| `basin.area_sq_mi`, `forest_frac`, `frac_snow`, `n_major_dams` | About, watershed text |
| `flood_categories[].category`, `stage_ft` | flood bar, flood-stage list, "ft below action" |
| `flood_categories[].flow_cfs` | flood bands on the flow chart, the forecast's flood category ("Rising to minor flood") |
| `geometry` `{ bounds, basin, rivers, gauges, dams }` | the basin map and its weather layers (hidden while null) |
| `climatology` (366 rows × 5 quantiles of flow and water temperature) | "Normal for the date" bands, the Now label ("Below normal"), "Normal flow today"; without it the status falls back to the flood category |
| `basin.travel_time_max_h`, `median_flow_cfs`, `n_dams` | watershed text, About |
| `watershed` (USGS WBD) | "The Upper Delaware and East Branch Delaware watersheds: …", About |

## `forecasts/{issue}.json`

| Field | Used for |
|---|---|
| `schema`, `issue`, `issue_time` | must match the pointer; "Forecast issued …", "Now" vs "Issued" |
| `flow` quantile series (hourly to 168 h) | flow chart, crest, Next 7 days slot |
| `temperature.hourly` (hourly to ≥ 168 h) | water-temperature chart; the view is hidden when `temperature` is null |
| `temperature.daily_max[].date`, `q50` | whether the week warms or cools (basin-layer pick); not drawn |
| `weather.bins.rain_mm`, `snow_mm`, `snowmelt_mm` (6 h) | rain and snowmelt strip, "What's driving it", basin-layer pick |
| `weather.past_rain` | past rain on the strip, "rain in the past day" |
| `weather.snowpack_swe_mm` | snowpack sentence and layer pick |

## `/api/visit` and `/api/status`

`POST /api/visit?site={id}` (empty body) once per load unless live.json says `active` and the site is awake for more
than 24 h (or always on); sent without live.json for a site that has never been
forecast. Reads `status`, `forecast` and `eta_s` (the warming-up loader's progress), or `error`: 409 `not_forecastable`
(the rule excluded the site since sites.json was written) shows "can't forecast" with `detail`'s reason text. `GET /api/status?site={id}` every 10 s while `waking`, for up to 3 minutes; reads
`status`, `forecast` and `run.started`, fetches `forecast.url` when `forecast.issue` changes, and loads live.json when a
first forecast appears.

## Not used from v1 (yet)

`recent_forecasts` (earlier/later runs), `story` (the page derives crest and trend from the quantiles itself),
`temperature.daily_max[].p_above_*`, `flood_probability`, `inputs`, `freshness`, `observations.stage`.
