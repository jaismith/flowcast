# Agent notes — Flowcast

## Cursor Cloud specific instructions

This repo is a **Yarn 4 monorepo** with workspaces `backend`, `client`, and `infra`. Dependency refresh is driven from the root; see [`package.json`](package.json).

- **Yarn 4**: Use Corepack so CLI matches `packageManager` (`yarn@4.0.2`). Global Yarn 1.x will not behave correctly with this repo.

```bash
corepack enable
corepack prepare yarn@4.0.2 --activate
```

- **Backend Python**: [`backend/pyproject.toml`](backend/pyproject.toml) pins **`python ~3.10`** (including Torch wheels locked for 3.10). If the VM only has newer Python, install 3.10 (for example `pip install --user uv` then `uv python install 3.10`), ensure `~/.local/bin` is on `PATH` for `uv` and `poetry`, then in `backend/` run **`poetry env use "$(uv python find 3.10)"`** once so `poetry install` succeeds when Yarn runs the workspace `install` script.

- **Lint / build / dev** (standard scripts live in workspace `package.json` files):

  - Client lint: `yarn workspace @flowcast/client lint`
  - Client production build: `yarn workspace @flowcast/client build`
  - Client dev: `yarn workspace @flowcast/client dev`
  - Infra TypeScript build: `yarn workspace @flowcast/infra build`
  - Infra CDK: `yarn workspace @flowcast/infra synth` / `deploy` (needs AWS credentials and env — see [`infra/README.md`](infra/README.md))

- **Tests**: `@flowcast/infra` exposes `yarn workspace @flowcast/infra test` (Jest). If [`infra/test/`](infra/test/) is absent, Jest exits with a configuration error until tests exist or config changes. There is **no** pytest/ruff suite wired for `backend` in `pyproject.toml`.

- **Runtime / APIs**: The Next.js app loads forecast data from **`ACCESS_API_ROOT`** in [`client/utils/constants.ts`](client/utils/constants.ts) (currently `https://api.flowcast.jaismith.dev`). **`next build`**, ISR, and dev navigations need **outbound HTTPS**. If the deployed API shape diverges from what [`client/utils/api.ts`](client/utils/api.ts) expects (e.g. `forecast` not an array), you may see console errors during data fetch even though pages can still return HTTP 200.

- **Secrets**: Full CDK deploy and some Lambda paths require AWS and external API credentials per infra/CI configuration — not needed for local `next dev` / `lint` / `client build` if the public API remains reachable.
