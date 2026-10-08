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

- `FLOWCAST_MOCK=snoozed|new|paused|delayed|excluded npm run dev` exercises the lazy-forecast states (a visit wakes
  a snoozed site for 20 s; `excluded` answers every visit 409 `not_forecastable`).
- `npm run dev:live` proxies `/data` and `/api` to the deployed backend (https://d2plrkhnzsjv1y.cloudfront.net);
  `FLOWCAST_API=https://<host> npm run dev` to any other.
- `npm run fixtures -- --from <checkout on cursor/simple-site-page-4752>` rebuilds the fixtures from the
  prototype's validation-year data (one issue, 2021-06-29 12Z).
- `node scripts/make-gauge-fixtures.mjs` refreshes the trimmed gauge catalog in `dev/fixtures/data/v1/gauges/`
  (the gauges around the fixture sites) from the deployed backend.
- `npm run typecheck` checks the contract types and loader.

## Routes

- `/` opens the last forecastable site viewed in this browser, else `sites.json`'s `default`.
- `/site/USGS-<number>` shows any site in `sites.json`, or any gauge in the national catalog (as a "can't forecast"
  page with the rule's reason); other USGS numbers get a "doesn't forecast" page, and anything else under `/site/`
  that isn't a slug gets "no gauge called …".
- `/site/<slug>`, `/site/<number>`, `/<number>`, `/USGS-<number>` (any case) and `?site=<number>` redirect to the
  canonical path.
- On CloudFront, page routes need rewriting to `index.html` (`deploy/spa-rewrite.js`, a viewer-request
  CloudFront Function).

## Site selector

The bar at the top of every page (`components/SiteBar.jsx`): search by river, town, state or USGS number (`/` or
⌘K focuses it) and a map of every gauge (`components/SiteMap.jsx`). Both open over the page, so nothing below moves.
Zoomed out, the map shows the `sites.json` sites; from zoom 6 it loads the national catalog tiles in view, so every
USGS gauge shows, drawn by whether it can be forecast. A gauge flowcast can't forecast opens a page with the reason.

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
export FLOWCAST_SITE_BUCKET=flowcast-site-257129854363-us-east-1 FLOWCAST_DISTRIBUTION_ID=E21E8HKTQXGQC9
scripts/deploy.sh --preview web      # https://d2plrkhnzsjv1y.cloudfront.net/preview/web/
scripts/deploy.sh                    # production (the root)
scripts/deploy.sh --dry-run …        # build and show the uploads only
```

Hashed assets get `max-age=31536000, immutable`, and `index.html` gets `no-cache`. Only `index.html` is invalidated. Previews read
the production `/data/` and `/api/`.
