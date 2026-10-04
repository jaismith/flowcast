/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of sites.json and sites/<id>.json (default /data/). */
  readonly VITE_DATA_URL?: string;
  /** Base URL of the visit and status endpoints (default /api/). */
  readonly VITE_API_URL?: string;
}
