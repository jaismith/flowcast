import type { ApiError, ApiStatus, Climatology, Forecast, ForecastPointer, Geometry, Live, QuantileSeries, Series, SiteId, SiteIndex, SiteSummary, Static, Status } from './contract';

// Absolute, so a preview deploy under /preview/<name>/ reads the production data and API.
const DATA = import.meta.env.VITE_DATA_URL ?? '/data/v1/';
const API = import.meta.env.VITE_API_URL ?? '/api/';
const ORIGIN_DATA = '/data/v1/';

export class ContractError extends Error {}

function check(cond: unknown, what: string): asserts cond {
  if (!cond) throw new ContractError(`site data: ${what}`);
}

/** A data file, or null if it isn't published (yet): 404, or 403 from an S3 origin that hides missing keys. */
async function getJSON<T>(url: string, init?: RequestInit): Promise<T | null> {
  const r = await fetch(url, { headers: { Accept: 'application/json' }, ...init });
  if (r.status === 404 || r.status === 403) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** An /api answer: the status body, or the API's error (unknown_site, not_supported). */
async function callApi(url: string, init?: RequestInit): Promise<ApiStatus | ApiError | null> {
  try {
    const r = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store', ...init });
    const body = await r.json().catch(() => null);
    if (r.ok || (body && 'error' in body)) return body;
    return null;
  } catch {
    return null;
  }
}

export const isApiError = (a: ApiStatus | ApiError | null): a is ApiError => !!a && 'error' in a;

/** Contract URLs are rooted at /data/v1/; VITE_DATA_URL can point the page elsewhere (e.g. a staging copy). */
const dataUrl = (path: string) => (path.startsWith(ORIGIN_DATA) ? DATA + path.slice(ORIGIN_DATA.length) : path);

const ms = (t: string | null | undefined) => (t ? Date.parse(t) : null);

// ---------------------------------------------------------------------------------------------- the page's model

/** A regular series in milliseconds: value k is at t0 + k * step. */
export interface S {
  t0: number;
  step: number;
  v: (number | null)[];
}
export interface Q {
  t0: number;
  step: number;
  q05: (number | null)[];
  q25: (number | null)[];
  q50: (number | null)[];
  q75: (number | null)[];
  q95: (number | null)[];
}

/** Everything one site's page draws, normalized from live.json, static.json and the current forecast. */
export interface SiteData {
  id: SiteId;
  name: string;
  short: string;
  river: string;
  /** "Callicoon, NY" */
  place: string;
  lat: number;
  lon: number;
  area_mi2: number;
  forest_frac: number | null;
  snow_frac: number | null;
  median_flow_cfs: number | null;
  travel_time_max_h: number | null;
  nid_dams: number | null;
  nid_major_dams: number | null;
  nws_lid: string | null;
  usgs_url: string;
  watershed: Static['watershed'] | null;
  floods: { category: string; stage_ft: number; flow_cfs: number | null }[];
  geo: (Geometry & { gauge: GeoJSON.Feature<GeoJSON.Point> }) | null;
  /** Day-major and flattened: value (day k, quantile q) is at k * 5 + q. */
  clim: { flow_cfs: (number | null)[]; water_temp_c: (number | null)[] | null } | null;
  obs: { flow: S | null; temp: S | null; stage: S | null };
  now: { t: number | null; flow_cfs: number | null; stage_ft: number | null; water_temp_c: number | null; stale: boolean };
  status: Status;
  awake_until: number | null;
  forecast: {
    issue: string;
    issued: number;
    flow: Q;
    temp: { hourly: Q; daily_max: NonNullable<Forecast['temperature']>['daily_max'] } | null;
    bins: { t0: number; step: number; rain_mm: (number | null)[]; snow_mm: (number | null)[]; snowmelt_mm: (number | null)[] | null } | null;
    past_rain: S | null;
    swe_mm: number | null;
  } | null;
}

const series = (s: Series | undefined | null): S | null => (s ? { t0: Date.parse(s.start), step: s.step_h * 3600e3, v: s.values } : null);
const qseries = (s: QuantileSeries): Q => ({ t0: Date.parse(s.start), step: s.step_h * 3600e3, q05: s.q05, q25: s.q25, q50: s.q50, q75: s.q75, q95: s.q95 });

// ---------------------------------------------------------------------------------------------- loading

export async function loadSiteIndex(): Promise<SiteIndex> {
  const idx = await getJSON<SiteIndex>(`${DATA}sites.json`);
  check(idx, 'sites.json not found');
  check(idx.schema === 'flowcast.sites/v1', `sites.json schema ${String(idx.schema)}, expected flowcast.sites/v1`);
  check(Array.isArray(idx.sites) && idx.sites.length, 'sites.json has no sites');
  check(idx.sites.some((s) => s.id === idx.default), `sites.json default ${String(idx.default)} is not one of its sites`);
  return idx;
}

export async function loadLive(id: SiteId): Promise<Live | null> {
  const live = await getJSON<Live>(`${DATA}sites/${id}/live.json`);
  if (!live) return null;
  check(live.schema === 'flowcast.live/v1' && live.id === id, `live.json for ${id}`);
  check(live.observations && live.now, 'live.observations / now');
  return live;
}

const staticCache = new Map<string, Promise<Static | null>>();
export function loadStatic(live: Live): Promise<Static | null> {
  const url = dataUrl(live.static_url);
  if (!staticCache.has(url)) staticCache.set(url, getJSON<Static>(url));
  return staticCache.get(url)!;
}

export async function loadForecast(p: ForecastPointer): Promise<Forecast | null> {
  const f = await getJSON<Forecast>(dataUrl(p.url));
  if (!f) return null;
  check(f.schema === 'flowcast.forecast/v1' && f.issue === p.issue, `forecast ${p.issue}`);
  const n = f.flow.q50.length;
  check(n >= 24 && (['q05', 'q25', 'q75', 'q95'] as const).every((k) => f.flow[k].length === n), 'forecast.flow quantiles');
  return f;
}

/** Normalizes the three files (and the site's sites.json entry) into the page's model. */
export function toSiteData(summary: SiteSummary, live: Live, st: Static | null, f: Forecast | null): SiteData {
  const b = st?.basin ?? {};
  const geometry = st?.geometry && st.geometry.basin && st.geometry.rivers ? st.geometry : null;
  const lat = st?.lat ?? summary.lat;
  const lon = st?.lon ?? summary.lon;
  const bins = f?.weather?.bins;
  return {
    id: summary.id,
    name: st?.name ?? summary.name,
    short: st?.short_name ?? summary.town,
    river: st?.river ?? summary.river,
    place: `${summary.town}, ${summary.state}`,
    lat,
    lon,
    area_mi2: b.area_sq_mi ?? summary.area_mi2,
    forest_frac: b.forest_frac ?? null,
    snow_frac: b.frac_snow ?? null,
    median_flow_cfs: b.median_flow_cfs ?? null,
    travel_time_max_h: b.travel_time_max_h ?? null,
    nid_dams: b.n_dams ?? null,
    nid_major_dams: b.n_major_dams ?? null,
    nws_lid: st?.nws_lid ?? null,
    usgs_url: st?.usgs_url ?? `https://waterdata.usgs.gov/monitoring-location/${summary.id}/`,
    watershed: st?.watershed ?? null,
    floods: st?.flood_categories ?? [],
    geo: geometry && { ...geometry, gauge: { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lon, lat] } } },
    clim: flatClimatology(st?.climatology),
    obs: { flow: series(live.observations.discharge), temp: series(live.observations.water_temperature), stage: series(live.observations.stage) },
    now: { t: ms(live.now.observed_at), flow_cfs: live.now.flow_cfs, stage_ft: live.now.stage_ft, water_temp_c: live.now.water_temp_c, stale: !!live.now.gauge_stale },
    status: live.status,
    awake_until: ms(live.awake_until),
    forecast: f && {
      issue: f.issue,
      issued: Date.parse(f.issue_time),
      flow: qseries(f.flow),
      temp: f.temperature?.hourly ? { hourly: qseries(f.temperature.hourly), daily_max: f.temperature.daily_max ?? [] } : null,
      bins: bins ? { t0: Date.parse(bins.start), step: bins.step_h * 3600e3, rain_mm: bins.rain_mm ?? [], snow_mm: bins.snow_mm ?? [], snowmelt_mm: bins.snowmelt_mm ?? null } : null,
      past_rain: series(f.weather?.past_rain),
      swe_mm: f.weather?.snowpack_swe_mm ?? null,
    },
  };
}

// ---------------------------------------------------------------------------------------------- lazy forecasting

/** 366 x 5 rows (v1) flattened day-major; null unless the flow climatology is complete. */
function flatClimatology(c: Climatology | null | undefined): SiteData['clim'] {
  const flat = (rows: (number | null)[][] | null | undefined) => (rows?.length === 366 && rows.every((r) => r.length === 5) ? rows.flat() : null);
  const flow = flat(c?.flow_cfs);
  return flow ? { flow_cfs: flow, water_temp_c: flat(c?.water_temp_c) } : null;
}

/**
 * Tells the backend someone is looking, which wakes a snoozed site (or forecasts a site for the first time),
 * unless live.json shows it awake for more than another day; always-on sites never need it. Once per site per
 * page load. Returns the visit answer (or the API's error), or null when no visit was sent or it failed.
 */
const visited = new Map<string, Promise<ApiStatus | ApiError | null>>();
export function postVisit(summary: SiteSummary, live: Live | null): Promise<ApiStatus | ApiError | null> {
  const id = summary.id;
  const awakeFor = live?.awake_until ? Date.parse(live.awake_until) - Date.now() : null;
  const awake = live?.status === 'active' && (live.always_on || (awakeFor != null && awakeFor > 24 * 3600e3));
  if (awake) return Promise.resolve(null);
  if (!visited.has(id)) visited.set(id, callApi(`${API}visit?site=${encodeURIComponent(id)}`, { method: 'POST', keepalive: true }));
  return visited.get(id)!;
}

export function loadStatus(id: SiteId): Promise<ApiStatus | ApiError | null> {
  return callApi(`${API}status?site=${encodeURIComponent(id)}`);
}
