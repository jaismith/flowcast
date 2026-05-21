![flowcast logo](https://github.com/jaismith/flowcast/blob/main/logo.png)

# Flowcast

Flowcast forecasts streamflow and water temperature at USGS monitoring sites, combining live gauge readings with weather data and machine learning. The web app charts historical observations, forward-looking predictions with confidence bands, and generates daily fishing-condition reports.

Live at [flowcast.jaismith.dev](https://flowcast.jaismith.dev) (API at `api.flowcast.jaismith.dev`).

## What it does

For each USGS site, Flowcast:

1. **Ingests data** — Pulls hourly water conditions (streamflow, water temperature) from USGS and atmospheric weather (precipitation, air temperature, cloud cover, snow) from Visual Crossing.
2. **Trains models** — Fits separate [NeuralProphet](https://neuralprophet.com/) time-series models per feature, using weather as future regressors.
3. **Forecasts** — Produces 7-day hourly predictions with 90% confidence intervals for streamflow and water temperature.
4. **Reports** — Generates a daily natural-language fishing report via Claude (AWS Bedrock), cached in DynamoDB.

New sites go through an onboarding pipeline (data fetch → DynamoDB export → model training on Fargate Spot → first forecast) with real-time progress over WebSocket.

## Repository layout

This is a Yarn 4 monorepo with three workspaces:

| Workspace | Path | Description |
|-----------|------|-------------|
| **client** | `client/` | Next.js 14 frontend (Mantine UI, Visx charts, Mapbox). Site pages at `/sites/[usgs_site]`. |
| **backend** | `backend/` | Python 3.10 service (Poetry). Handlers for data updates, forecasting, training, API access, and site onboarding. |
| **infra** | `infra/` | AWS CDK (TypeScript) stack defining all cloud resources and deploying the client via `cdk-nextjs-standalone`. |

Research and experimentation live in `notebooks/` (model exploration, validation, data prep).

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Next.js    │────▶│  Access Lambda   │────▶│   DynamoDB      │
│  (CloudFront)│     │  (REST API)      │     │  flowcast-data  │
└─────────────┘     └────────┬─────────┘     │  flowcast-sites │
                             │               │  flowcast-reports│
                             ▼               └────────▲────────┘
                    ┌──────────────────┐               │
                    │ Step Functions   │───────────────┘
                    │ update → export  │
                    │ → train → fcst   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Update Lambda  Batch (Fargate)  Forecast Lambda
        (USGS + weather)  (NeuralProphet)  (inference)
              │                              │
              └──────────────┬───────────────┘
                             ▼
                        S3 (archives, models)
```

**Scheduled jobs**

- **Hourly** — Step Functions runs update → forecast for the primary site.
- **Weekly** — DynamoDB table export to S3 for model training archives.

**Onboarding** — `POST /site/register` starts the same pipeline with `is_onboarding: true`. DynamoDB stream events push status and logs to subscribed WebSocket clients.

## Backend handlers

| Handler | Runtime | Role |
|---------|---------|------|
| `update` | Lambda | Fetch new USGS + weather observations, write to DynamoDB |
| `export` | Lambda | Export DynamoDB snapshot to S3 for training |
| `train` | AWS Batch (Fargate Spot) | Train NeuralProphet models per feature, store in S3 |
| `forecast` | Lambda | Run inference using trained models + weather forecast rows |
| `access` | Lambda | REST API (`/forecast`, `/site`, `/site/register`, `/report`) |
| `onboard` | Lambda + WebSocket | Track onboarding subscriptions and stream progress |

## Getting started

### Prerequisites

- Node.js 18+ and [Yarn 4](https://yarnpkg.com/) (`corepack enable`)
- Python 3.10 and [Poetry](https://python-poetry.org/)
- AWS CLI configured (for deploy)
- Environment variables for external APIs (see below)

### Install

```bash
yarn install
cd backend && poetry install
```

### Local development

**Frontend**

```bash
yarn workspace @flowcast/client dev
```

**Backend** — handlers can be invoked locally via `python -m index <target>` from `backend/src/` (requires AWS credentials and env vars):

```bash
cd backend/src
poetry run python -m index access   # test access handler
poetry run python -m index update   # test update handler
```

### Deploy

Deploys automatically on push to `main` via GitHub Actions. Manual deploy:

```bash
yarn deploy
```

Requires these secrets/env vars (also used in CI):

| Variable | Purpose |
|----------|---------|
| `WEATHER_KEY` | Visual Crossing API key |
| `VISUAL_CROSSING_API_KEY` | Visual Crossing (alternate) |
| `RDS_HOST` / `RDS_PASS` | Legacy weather data source |
| `NCEI_HOST` / `NCEI_EMAIL` | NOAA NCEI access |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | AWS deployment |

## API

Base URL: `https://api.flowcast.jaismith.dev`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/forecast?usgs_site=&start_ts=&historical_fcst_horizon=` | Time-series forecast + history |
| `GET` | `/site?usgs_site=` | Site metadata (name, coordinates, status) |
| `POST` | `/site/register?usgs_site=` | Start onboarding; returns WebSocket progress URL |
| `GET` | `/report?usgs_site=` | Daily fishing report (generated on first request) |

## Key constants

Defined in `backend/src/utils/constants.py`:

- **Forecast horizon:** 168 hours (7 days)
- **Features forecasted:** `streamflow`, `watertemp`
- **Confidence interval:** 90%
- **Default site:** `01427510`

## Status

Active development. The root README and sub-workspace READMEs may lag behind the code; the infra stack in `infra/lib/flowcast.ts` is the source of truth for deployed architecture.
