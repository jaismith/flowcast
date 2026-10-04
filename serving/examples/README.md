# Example payloads (Callicoon)

Files in the layout the site serves under `/data/v1/` (see `../schema/README.md`):

| File | Served at |
|---|---|
| `sites.json` | `/data/v1/sites.json` |
| `USGS-01427510/live.json` | `/data/v1/sites/USGS-01427510/live.json` |
| `USGS-01427510/static.json` | `/data/v1/sites/USGS-01427510/static.json` |
| `USGS-01427510/forecasts/2026100400.json` | `/data/v1/sites/USGS-01427510/forecasts/2026100400.json` |
| `api.json` | `visit` and `status` bodies of `/api/visit` and `/api/status` |

- `live.json` holds real USGS observations for Oct 1–4, 2026 (the deployed file carries the last 30 days).
- The forecast is synthetic (`"example"` field) with the real shape: hourly flow quantiles for 168 h, hourly water
  temperature for 180 h, daily highs, 6-hourly GEFS bins. Live files have no `example` field.
- `static.json` has identity and NWS flood stages; basin facts, flood flows and `geometry` arrive with onboarding.
- `sites.json` is the real index: all 553 basins the flow model covers, with the four served sites
  (Callicoon, Lordville, Allagash, Accotink) `forecast_ready`.
- `gauges/index.json` is the published catalog index; `gauges/ids.json` is a 4-gauge excerpt of the id -> tile map; `gauges/tiles/-80_40.json` is a trimmed tile (6 of its 524
  gauges: Callicoon, Fishs Eddy (a model basin whose gauge stopped: ineligible, no_recent_discharge), an eligible
  gauge and ineligible ones with their reasons).
