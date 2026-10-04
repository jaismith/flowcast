# flowcast serving

The production backend: live forecasts, lazy wakes, the visit/status API and the per-site JSON the page reads.
Design: the project's `production-architecture.md`; data contract: [`schema/`](schema/README.md).

## Pieces (stack `flowcast-serve`, `infra-v2/lib/serving.ts`)

| Piece | What it does |
|---|---|
| CloudFront (one distribution) | `/` and page routes from the site bucket (SPA rewrite `web/deploy/spa-rewrite.js`), `/data/*` from the data bucket, `/api/*` to the API function |
| `flowcast-serve-api` | `POST /api/visit`, `GET /api/status`: snooze window, visit cap, wake runs |
| `flowcast-forecast` (container, arm64) | Forecast runs (`forecast.py`) for cycles and wakes; daily SNODAS |
| `flowcast-serve-ops` | Cycles at 01:30/07:30/13:30/19:30 UTC; hourly light build (live.json, sites.json); daily gauge catalog |
| `flowcast-control` (DynamoDB) | Per-site state and per-run locks (`control.py`) |
| Lake `models/` | Model registry: `production.json` points at flow / temp / snow versions |

## Deploy

```bash
serving/deploy.sh            # cdk deploy flowcast-serve; prints the site bucket and distribution id
```

The page is deployed by the web app's own script (PR #65) into the site bucket; previews go under
`/preview/<name>/` and are routed by the same CloudFront Function.

## Operate

```bash
export LAKE_URI=s3://flowcast-v2-lake-<account>-<region> DATA_BUCKET=flowcast-data-<account>-<region>
flowcast-serve promote flow --version <v> --runs s3://…/runs/<seed> … --cube <training cube> --calibration <dir>
flowcast-serve activate --flow <v>               # swap models (takes effect at the next run); `history` lists pointers
flowcast-serve check-gauges                      # exit 1 if production's outflow/temperature gauge lists differ from training
flowcast-serve rebuild-gauges flow --from <v> --version <v2>   # same weights, outflows.json rebuilt from the cube
flowcast-serve onboard --plans … --meta … --hrus …   # per-basin weights, HRUs, NWPS stages
flowcast-serve build-static --cube … --meta …        # static.json: geometry, watershed, climatology, rated flood flows
flowcast-serve build-index --selection selection.parquet
flowcast-serve run --sites callicoon --issue 2026100412   # one forecast in this process
flowcast-serve alerts --site callicoon --delta 1          # alerts hook: +1/-1 subscription (always-on while > 0)
flowcast-serve parity-model … / parity-extract …          # gates 1-2 on validation years
```

`build-static` and `build-eligibility` call the USGS API many times and share its 1,000-requests-an-hour key with
production; run them with few workers.
