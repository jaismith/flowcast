# flowcast landing pages (prototype)

Per-location pages that show what each gauge is doing now, how flowcast's model forecast real events, how skillful those forecasts are, and the basin behind the gauge. There's an overview map of every model basin, plus pages for three contrasting rivers:

| Site | Why it's here |
|---|---|
| Delaware River at Callicoon, NY (`01427510`) | The demo site. A big river below NYC's reservoirs, with an NWS (MARFC) forecast to compare against. It will eventually replace the old live site. |
| Allagash River near Allagash, ME (`01011000`) | A snowmelt river: the spring freshet is the big event of the year. |
| Accotink Creek near Annandale, VA (`01654000`) | A small, flashy suburban creek (23 mi², 72% developed). |

This is a lightweight design prototype. It's a standalone Vite app, and nothing in `client/`, `backend/`, `infra/` or `infra-v2/` changes.

## Run it

```bash
cd prototypes/landing
npm install
npm run dev          # http://localhost:5173 (overview) and /site.html?site=01427510
```

`npm run build` writes a static site to `dist/`. Paths are relative, so it can be hosted anywhere.

The data in `public/data/` (about 9 MB) is committed, so the pages run without AWS access. On page load the hero cards refresh from the USGS Water Data API (`latest-continuous`, CORS-enabled, keyless) and fall back to the committed snapshot if that fails.

## Pages

**Overview (`index.html`).**
- A MapLibre map of all 554 model gauges, colored by today's flow against normal for the date (WaterWatch-style classes; percentiles from 2000–2022 daily means).
- It can also be colored by snow share or flashiness.
- Featured rivers link to their pages, and each featured card has a mini basin map.

**Site page (`site.html?site=<id or slug>`).**
- **Hero:** a basin map (NLDI basin outline with the outside masked; the NHDPlus river network, wider for higher stream order; reservoir-release and upstream gauges; NID dams sized by storage; terrain hillshade) and live "now" cards for flow, water temperature and river level, each against normal.
  - An optional *Forecast rain* layer fills the basin with the 7-day GEFS total for the selected replay forecast.
- **Last 30 days:** observed flow or water temperature over the normal range for each date.
- **Forecast replay:**
  - Pick any forecast issued in WY2021–2022 with the scrubber, the arrow keys or the event chips (biggest floods, rain on snow, snowmelt, heat waves).
  - Flow and water-temperature fan charts show the 50% and 90% ranges and what actually happened. At Callicoon the NWS bulletin is overlaid where one exists.
  - Below the flow chart is the GEFS basin rain and snow behind the forecast.
  - The *peak evolution* chart shows how the forecast for the window's highest flow changed over the 7 days before it. `?issue=<unix seconds>` makes a view shareable.
- **Skill:**
  - CRPS skill vs persistence by lead time (1 h – 7 days), with 95% block-bootstrap intervals and the 552-river median.
  - Water-temperature skill, and the 90%-range hit rate.
  - At Callicoon, skill against NWS/MARFC at its own bulletin times. Elsewhere, a reliability chart.
- **Basin:** facts (area, elevation, land cover, snow share, flashiness, travel time, dams, upstream gauges), and SNODAS snowpack against flow.

The `°F` button switches between US and metric units, and the choice is remembered.

## Data rules

- **Model forecasts come only from the validation years, WY2021–2022** (Oct 2020 – Sep 2022). The frozen test years (WY2023+) are never read. `build_data.py` reads `trainval.zarr`, which stops at 2022-09-30 23:00 UTC, and it fails if any forecast, GEFS or MARFC timestamp falls outside WY2021–2022.
- **Flow:** the final three-seed SNODAS model (`full-v2-snodas-s42/43/44`).
  - Its saved CMAL mixtures are pooled exactly as `mixture-ensemble` does it (PR #47): 4 draws per seed × 11 GEFS members, so 132 samples.
  - They are calibrated with the PR #51 standard-path conditional stretch. Each issue uses the fit that held out its own water year, and the tail boost is off.
- **Water temperature:** model v1, seeds 42 + 44 (40 coherent members), with the PR #51 `temp-score` calibration.
  - Hourly values get a per-lead offset and spread factor.
  - Daily highs get the GEFS warm-up offset, by regulation and season.
  - The hourly fan stops at 48 h because stored leads are 12-hourly beyond that; daily highs cover days 0–7.
- **Observations** may come from any period: the cube (WY2001–2022) for climatology and replay truth, and live USGS for "now".

## Rebuilding the data

Needs read access to the flowcast S3 buckets, plus the PR #51 calibration parameters (`flow/` and `temperature/` directories from `flowcast-model flow-calibrate` and `temp-score`).

```bash
uv venv -p 3.12 && . .venv/bin/activate
uv pip install -e ../../pipeline -e ../../evaluation s3fs xarray "zarr>=3" shapely scipy fastparquet
python scripts/build_data.py --calibration <params dir>      # all three sites + overview
python scripts/build_data.py --sites 01654000 --skip-overview  # one site
```

The build does the following:
- It reads about 450 MB of mixtures and temperature hindcasts, caching them in `--cache` (default `/tmp/lp-cache`).
- It also reads cube slices, plus the MARFC archive for Callicoon.
- It calls the USGS Water Data API, NLDI and GeoServer, and the NID feature service, none of which need a key.
- The overview reads daily flows for all 554 basins once and caches them.

Featured sites and their copy live in `scripts/sites.py`.
