/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the v1 data files (default /data/v1/). */
  readonly VITE_DATA_URL?: string;
  /** Base URL of the visit and status endpoints (default /api/). */
  readonly VITE_API_URL?: string;
  /** Site shown at `/` until sites.json carries `default`. */
  readonly VITE_DEFAULT_SITE?: string;
}
