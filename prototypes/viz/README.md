# flowcast visualization prototypes

Seven standalone, interactive sketches for the upper Delaware River above the Callicoon gauge (USGS 01427510). They explore how a rebuilt flowcast could show where water comes from and how confident the forecast is. This folder is independent of the app in `client/`, `backend/` and `infra/`, and nothing here touches them.

## Run it

```bash
cd prototypes/viz
npm install
npm run dev          # http://localhost:5173 (gallery index)
```

`npm run build` writes a static site to `dist/`, and `npm run preview` serves it. The site uses relative paths, so `dist/` can be hosted anywhere (S3, GitHub Pages, and so on).

The repo root is a Yarn 4 workspace, but this folder isn't part of it. It uses its own `npm` lockfile so it can't disturb the app's dependencies.

## Prototypes

| # | Page | Approach | Stack |
|---|------|----------|-------|
| 1 | `river-pulse/` | Water parcels flow down 618 NHDPlus reaches. Reach discharge = nearest downstream gauge × drainage-area ratio. Color by flow-vs-normal or water temperature. | D3, Canvas 2D |
| 2 | `watershed-3d/` | Real terrain with a year of daily weather (rain, snowfall, snowpack, solar radiation and air temperature, from ECMWF IFS) draped on it, plus falling rain and snow particles. | three.js, GLSL |
| 3 | `forecast-fan/` | flowcast's live 7-day forecast unfurls as the horizon advances, linked to a GEFS precipitation plume and a 30-day GloFAS ensemble ridgeline. | D3 |
| 4 | `storm-explorer/` | A year of hourly hyetograph and hydrograph with linked brushing, Lyne–Hollick baseflow separation, auto-detected events, and a rain-vs-runoff scatter. | D3 |
| 5 | `river-year/` | A radial climatology (50 years of percentile bands vs this year) that morphs into a spiral of every day since 1975. | D3, Canvas 2D |
| 6 | `raindrop-journey/` | Scrollytelling: follow one drop along the real NLDI flow path, from a Catskills hillside through Cannonsville Reservoir to the gauge. | MapLibre GL 3D terrain |
| 7 | `flood-wave/` | A joy-plot of all 27 gauges from headwaters to outlet, with a time sweep that shows the flood wave traveling downstream. | D3 |

Most pages accept URL parameters for reproducible views, for example `watershed-3d/?day=2026-07-28&mode=sun&paused`, `forecast-fan/?h=100`, `river-year/?view=spiral`, and `storm-explorer/?event=2`. The space bar toggles play and pause on the animated pages.

## Data

Every data file is real public data, fetched by `scripts/fetch-data.mjs` and cached in `public/data/` (about 2.2 MB). Refresh it with:

```bash
npm run fetch-data   # ~1 min, no API keys needed
```

| File | Source |
|------|--------|
| `basin.json` | USGS NLDI basin for `nwissite/USGS-01427510` |
| `rivers.json`, `waterbodies.json` | NHDPlus V2 flowlines (stream order ≥ 2, upstream of the gauge via NLDI) and waterbodies from the USGS `wmadata` GeoServer, including EROM mean flow and velocity |
| `gauges-storm.json`, `gauges-recent.json` | USGS NWIS instantaneous values (discharge and water temperature) for every active gauge upstream of Callicoon, hourly means. "Storm" is centered on the largest flow of the past year |
| `callicoon-hourly.json`, `callicoon-daily.json` | NWIS IV for the past year; NWIS daily values since Oct 1975 |
| `weather-basin-hourly.json`, `weather-grid-daily.json` | Open-Meteo historical API, `models=ecmwf_ifs` (ECMWF IFS 9 km analysis; this is what `best_match` resolves to for 2017+), at 42 points inside the basin |
| `terrain.png` / `.json` | AWS Terrain Tiles (terrarium, z10), cropped and resampled |
| `forecast-flowcast.json` | A read-only `GET https://api.flowcast.jaismith.dev/forecast?usgs_site=01427510`. The script never calls `/report`, which triggers paid Bedrock calls |
| `forecast-glofas.json` | Open-Meteo Flood API (GloFAS v4, 51 members). The script picks the grid cell whose recent flow matches the gauge |
| `forecast-gfs-ensemble.json` | Open-Meteo Ensemble API (GEFS, 31 members) at the basin centroid |
| `raindrop-path.json` | NLDI downstream-mainstem navigation from a hillslope point near Stamford, NY, plus NHDPlus attributes |

Two things are derived rather than measured, and both are labeled on screen:

- **The inner 50% band in Forecast Fan.** The API only returns the 5th and 95th percentiles, so the inner band assumes a (log-)normal spread.
- **Reach-level discharge in River Pulse.** It's estimated from gauges with the drainage-area ratio method.

The scrollytelling page streams Esri World Imagery and AWS terrain tiles at runtime. Every other page works from the cached files alone.

## Code layout

```
index.html, gallery.js     gallery page (animated river-network background)
shared/common.js           data loading, formatting (Eastern time), tooltip, hi-DPI canvas
shared/network.js          NHDPlus network graph + gauge→reach snapping
shared/style.css           shared dark theme
<prototype>/index.html     one folder per prototype (Vite multi-page build)
scripts/fetch-data.mjs     build-time data fetch and cache
public/data/               cached data (committed)
public/thumbs/             gallery thumbnails
```
