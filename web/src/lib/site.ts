import type { Series, SiteBundle, SiteIndex, SiteStatus } from './contract';

// Absolute, so a preview deploy under /preview/<name>/ reads the same data and API as production.
const DATA = import.meta.env.VITE_DATA_URL ?? '/data/';
const API = import.meta.env.VITE_API_URL ?? '/api/';

export class ContractError extends Error {}

async function getJSON(url: string): Promise<unknown> {
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

/** Throws a ContractError naming the first missing or mistyped field, so a backend mismatch fails loudly. */
function check(cond: unknown, what: string): asserts cond {
  if (!cond) throw new ContractError(`site data: ${what}`);
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isSeries = (s: unknown): s is Series => !!s && isNum((s as Series).t0) && isNum((s as Series).step_h) && Array.isArray((s as Series).v);

export async function loadSiteIndex(): Promise<SiteIndex> {
  const idx = (await getJSON(`${DATA}sites.json`)) as SiteIndex | null;
  check(idx, 'sites.json not found');
  check(idx.schema === 'flowcast.sites.v0', `sites.json schema ${String(idx.schema)}, expected flowcast.sites.v0`);
  check(Array.isArray(idx.sites) && idx.sites.length, 'sites.json has no sites');
  check(idx.sites.some((s) => s.id === idx.default), `default site ${idx.default} is not in sites.json`);
  return idx;
}

/** The site's bundle, or null if the backend has not written one yet (a site that has never been forecast). */
export async function loadSite(id: string): Promise<SiteBundle | null> {
  const b = (await getJSON(`${DATA}sites/${encodeURIComponent(id)}.json`)) as SiteBundle | null;
  if (!b) return null;
  check(b.schema === 'flowcast.site.v0', `schema ${String(b.schema)}, expected flowcast.site.v0`);
  check(b.site?.id === id, `site.id ${String(b.site?.id)} for ${id}`);
  for (const k of ['area_mi2', 'median_flow_cfs', 'travel_time_max_h', 'lat', 'lon'] as const) check(isNum(b.site[k]), `site.${k}`);
  check(b.geo?.bounds?.length === 4 && b.geo.basin && b.geo.gauge && b.geo.rivers && b.geo.gauges && b.geo.dams, 'geo');
  check(b.climatology?.flow_cfs?.length === 366 * 5, 'climatology.flow_cfs (366 x 5)');
  check(isSeries(b.observed?.flow_cfs) && isSeries(b.observed?.precip_mm), 'observed.flow_cfs / precip_mm');
  const f = b.flow_forecast;
  if (f) {
    check(isNum(f.issued_at) && f.flow_cfs.length === f.leads_h.length * f.quantiles.length, 'flow_forecast (leads x quantiles)');
    const p = f.precip;
    check(p && p.mean_mm.length === p.snow_share.length && (p.melt_mm == null || p.melt_mm.length === p.mean_mm.length), 'flow_forecast.precip');
  }
  const t = b.water_temp_forecast;
  if (t) {
    check(isNum(t.issued_at) && t.temp_c.length === t.leads_h.length * t.quantiles.length, 'water_temp_forecast (leads x quantiles)');
    check(t.daily_high.temp_c.length === t.daily_high.dates.length * t.quantiles.length, 'water_temp_forecast.daily_high');
  }
  return b;
}

const visited = new Set<string>();

/** Tells the backend someone is looking, which wakes a snoozed site's forecasting; once per site per page load. Failures are ignored. */
export function postVisit(id: string): void {
  if (visited.has(id)) return;
  visited.add(id);
  fetch(`${API}visit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ site: id }), keepalive: true }).catch(() => {});
}

export async function loadStatus(id: string): Promise<SiteStatus | null> {
  try {
    return (await getJSON(`${API}status?site=${encodeURIComponent(id)}`)) as SiteStatus | null;
  } catch {
    return null;
  }
}
