# flowcast serving data contract (v1)

What the river page reads. Everything is served by one CloudFront distribution; the page makes no other data
calls (basemap and terrain tiles aside). Schemas are JSON Schema 2020-12; examples are in `../examples/`.

## URLs

| URL | Schema | Cache-Control | Written |
|---|---|---|---|
| `/data/v1/sites.json` | `sites.schema.json` | `max-age=60` | Hourly, every registry site |
| `/data/v1/sites/{site}/live.json` | `live.schema.json` | `max-age=60` | Hourly (observations), after each forecast run, and on state changes (visit, wake) |
| `/data/v1/sites/{site}/static.json` | `static.schema.json` | `max-age=300` | At onboarding |
| `/data/v1/sites/{site}/forecasts/{issue}.json` | `forecast.schema.json` | `max-age=31536000, immutable` | Once per forecast run; kept 30 days |
| `POST /api/visit?site={site}` | `api.schema.json#/$defs/visit` | `no-store` | — |
| `GET /api/status?site={site}` | `api.schema.json#/$defs/status` | `no-store` | — |

- `{site}` is the slug (`callicoon`, `lordville`, `allagash`, `accotink`). The USGS id is in the payloads.
- `{issue}` is the forecast's issue time as `YYYYMMDDHH` (UTC), e.g. `2026100412` is 12Z Oct 4, 2026.
- Page routes: `/site/{site}` serves the single-page app's `index.html` (CloudFront Function); previews live under
  `/preview/{name}/` with the same routing (`/preview/{name}/site/{site}`), reading the production `/data/` files.

## Units and times

- Times are ISO 8601 UTC with a `Z` suffix. Hourly series are hour-ending: the value at `t` covers `(t − 1 h, t]`,
  the USGS and model convention.
- Flow is ft³/s, stage ft, water temperature °C, rain, snow and snowmelt mm. The page converts for display.
- Regular series are `{start, step_h, values}`: value `i` is at `start + i × step_h` hours. Missing values are `null`.
- Quantile series share one time axis: `q05`, `q25`, `q50`, `q75`, `q95` arrays of equal length.

## Page flow

1. Fetch `live.json` (and `static.json` once). Draw observations, then fetch `live.forecast.url` (immutable).
2. Call `POST /api/visit?site=…` on load unless `live.state` is `active` and `live.awake_until` is more than 24 h away
   (always-on sites have `awake_until: null` and never need the call).
3. If the visit response (or `live.state`) is `waking`, poll `GET /api/status?site=…` every 10 s. When
   `status.forecast.issue` changes, fetch the new `status.forecast.url` and swap it in without a reload.
   Stop polling when `status.state` is no longer `waking` (or after 3 minutes, then show the paused banner).

States (`state` in live, visit and status):

| State | Meaning | Banner |
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
