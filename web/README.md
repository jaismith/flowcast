# flowcast web

The river forecast pages: one page per gauge with the week's flow and water-temperature forecasts, the weather
behind them, and the basin. Vite, React, Tailwind v4, Observable Plot and MapLibre. Promoted from
`prototypes/river-page` (branch `cursor/simple-site-page-4752`).

## Develop

```bash
cd web
npm ci
npm run dev                          # http://localhost:5174/ (strict: fails rather than moving ports)
```

`npm run dev` serves the fixtures in `dev/fixtures/` through a mock of the serving backend (`dev/mock-api.js`):

- `FLOWCAST_MOCK=snoozed|new|paused|delayed npm run dev` exercises the lazy-forecast states (a visit wakes a
  snoozed site for 20 s).
- `FLOWCAST_API=https://<host> npm run dev` proxies `/data` and `/api` to a real backend instead.
- `npm run fixtures -- --from <checkout on cursor/simple-site-page-4752>` rebuilds the fixtures from the
  prototype's validation-year data (one issue, 2021-06-29 12Z).
- `npm run typecheck` checks the contract types and loader.

## Routes

- `/` shows the default site, and `/site/USGS-<number>` shows any site in `sites.json`.
- `/site/<slug>`, `/site/<number>`, `/<number>`, `/USGS-<number>` and `?site=<number>` redirect to the canonical
  path.
- On CloudFront, page routes need rewriting to `index.html` (`deploy/spa-rewrite.js`, a viewer-request
  CloudFront Function).

## Data

Everything comes from the serving backend's v1 contract, from the same origin: `/data/v1/…` and
`/api/visit`, `/api/status`. See `CONTRACT.md` for each field the page reads and what it still needs.

The page also calls these third-party services from the browser:

- OpenFreeMap vector tiles and style
- AWS Terrain Tiles for the hillshade
- Open-Meteo for the basin weather layers
- rsms.me for the Inter font

## Build and deploy

```bash
npm run build                        # dist/: index.html + assets/[name]-[hash].{js,css}
FLOWCAST_SITE_BUCKET=… FLOWCAST_DISTRIBUTION_ID=… scripts/deploy.sh                    # production
FLOWCAST_SITE_BUCKET=… FLOWCAST_DISTRIBUTION_ID=… scripts/deploy.sh --preview <name>   # /preview/<name>/
scripts/deploy.sh --dry-run …        # build and show the uploads only
```

Hashed assets get `max-age=31536000, immutable`, and `index.html` gets `no-cache`. Only `index.html` is invalidated. Previews read
the production `/data/` and `/api/`.
