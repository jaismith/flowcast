# What the page reads from the serving backend

Against the v1 contract in `serving/schema/` (PR #64). Types: `src/lib/contract.ts`; checks and normalization:
`src/lib/site.ts`. **Requested** fields are not in v1 yet. They would be additive, and the page works without
them by dropping the part noted.

## `/data/v1/sites.json`

| Field | Used for |
|---|---|
| `schema` | must be `flowcast.sites/v1` |
| `sites[].id`, `slug` | routes: `/site/USGS-…` is canonical; `/site/<slug>` resolves to it |
| `sites[].name`, `river`, `town`, `state` | page title, header (`<river> at <town>, <state>`), not-found and not-served pages |
| `sites[].lat`, `lon`, `area_mi2` | fallbacks when static.json is missing |
| `sites[].forecast_ready` | `false` shows "covered but not served yet" |
| **Requested:** `default` | the site at `/`; until then the build's `VITE_DEFAULT_SITE` (USGS-01427510) |

## `/data/v1/sites/{id}/live.json`

| Field | Used for |
|---|---|
| `schema`, `id` | must be `flowcast.live/v1` and match the route |
| `status`, `always_on`, `awake_until` | whether to POST a visit; the banners |
| `forecast` (`issue`, `issue_time`, `url`) | the forecast to fetch; whether to keep drawing it while waking (≤ 3 days old) |
| `static_url` | static.json |
| `now.observed_at`, `flow_cfs`, `stage_ft`, `water_temp_c`, `gauge_stale` | the Now slot, river level and flood bar, water temperature, "Updated …" |
| `observations.discharge` (hourly) | the past 3 days on the flow chart; 24 h trend; "Now" at the forecast's issue |
| `observations.water_temperature` (hourly) | the past 3 days on the temperature chart |

## `/data/v1/sites/{id}/static.json`

| Field | Used for |
|---|---|
| `name`, `short_name`, `river`, `lat`, `lon`, `nws_lid`, `usgs_url` | header, headings, About |
| `basin.area_sq_mi`, `forest_frac`, `frac_snow`, `n_major_dams` | About, watershed text |
| `flood_categories[].category`, `stage_ft` | flood bar, flood-stage list, "ft below action" |
| `flood_categories[].flow_cfs` | **needs values** (null in the example): flood bands on the flow chart, the forecast's flood category ("Rising to minor flood") |
| **Requested:** `geometry` as `{ bounds, basin, rivers, gauges, dams }` | the basin map and its weather layers (hidden while null). `rivers`: LineStrings digitized upstream to downstream with `order` (Strahler); `gauges`: `name`, `active`, `q`; `dams`: `name`, `river`, `year`, `storage_af`; `bounds`: `[w, s, e, n]` |
| **Requested:** `climatology` `{ quantiles: [0.1, 0.25, 0.5, 0.75, 0.9], flow_cfs: 366×5, water_temp_c: 366×5 }`, day-of-year quantiles of the daily mean | "Normal for the date" bands on both charts, the Now label ("Below normal"), "Normal flow today". Without it the status falls back to the flood category |
| **Requested:** `basin.travel_time_max_h` | "Water from the headwaters takes up to N days" |
| **Requested:** `basin.median_flow_cfs` | About: typical flow |
| **Requested:** `basin.n_dams` | About: "3 of 98" major dams |
| **Requested:** `watershed` `{ level, huc, name, parts: [{ huc, name }], source }` (USGS WBD) | "The Upper Delaware and East Branch Delaware watersheds: …", About |

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

`POST /api/visit?site={id}` (empty body) once per load unless `active` and awake for more than 24 h; reads
`status` and `forecast`. `GET /api/status?site={id}` every 10 s while `waking`, for up to 3 minutes; reads
`status` and `forecast`, and fetches `forecast.url` when `forecast.issue` changes.

## Not used from v1 (yet)

`recent_forecasts` (earlier/later runs), `story` (the page derives crest and trend from the quantiles itself),
`temperature.daily_max[].p_above_*`, `flood_probability`, `inputs`, `freshness`, `observations.stage`.
