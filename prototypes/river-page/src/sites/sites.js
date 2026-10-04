import { stateName } from './gauges.js';

// The serving contract (serving/schema/ on cursor/production-backend-30da). Everything is same-origin: CloudFront
// in production, the dev server's proxy locally (vite.config.js).
const SITES_URL = '/data/v1/sites.json';

export const DEFAULT_SITE = 'USGS-01427510';
const LAST_SITE_KEY = 'flowcast:last-site';
const DAY = 24 * 3600 * 1000;

// ---------------------------------------------------------------------------------------------- ids and routes

/** "01427510", "usgs-01427510" or "USGS-01427510" → "USGS-01427510"; anything else → null. */
export function canonicalId(raw) {
  const m = /^(?:usgs-?)?(\d{8,15})$/i.exec(String(raw ?? '').trim());
  return m ? `USGS-${m[1]}` : null;
}

/** "USGS-01427510" → "01427510", the key the per-site data files use. */
export const usgsNumber = (id) => id.replace(/^USGS-/, '');

export const sitePath = (id) => `/site/${id}`;

/** The site a URL asks for: `/site/<id>`, or the older `?site=<number>`. Anything else (a slug, a typo) is returned as typed. */
export function siteFromUrl(pathname = location.pathname, search = location.search) {
  const m = /^\/site\/([^/]+)\/?$/.exec(pathname);
  if (m) {
    const raw = decodeURIComponent(m[1]);
    return canonicalId(raw) ?? raw;
  }
  return canonicalId(new URLSearchParams(search).get('site'));
}

export function lastSite() {
  try {
    return localStorage.getItem(LAST_SITE_KEY);
  } catch {
    return null;
  }
}

export function rememberSite(id) {
  try {
    localStorage.setItem(LAST_SITE_KEY, id);
  } catch {
    // private mode; the default site is fine
  }
}

// ---------------------------------------------------------------------------------------------- index

let sitesPromise = null;

/**
 * Every site flowcast can forecast (sites.schema.json: the 553 model basins), as the same site objects gauges.js
 * makes for catalog gauges. Fetched once per page load.
 */
export function loadSites() {
  sitesPromise ??= fetch(SITES_URL)
    .then((r) => {
      if (!r.ok) throw new Error(`sites index: ${r.status}`);
      return r.json();
    })
    .then((j) => j.sites.map(fromIndex))
    .catch((e) => {
      sitesPromise = null;
      throw e;
    });
  return sitesPromise;
}

function fromIndex(e) {
  return {
    id: e.id,
    usgsId: e.id.replace(/^USGS-/, ''),
    slug: e.slug ?? null,
    name: e.name,
    river: e.river,
    town: e.town,
    region: e.state,
    lat: e.lat,
    lon: e.lon,
    areaMi2: e.area_mi2,
    inIndex: true,
    hasForecast: e.forecast_ready,
    status: e.status ?? null,
    alwaysOn: !!e.always_on,
    inTrainingRegion: e.in_training_region,
    forecastable: true,
    temperature: !!e.has_temp,
    reason: null,
  };
}

export const placeOf = (s) => [s.town, s.region].filter(Boolean).join(', ');
export const titleOf = (s) => (s.town ? `${s.river} at ${s.town}` : s.river);

// ---------------------------------------------------------------------------------------------- search

const normalize = (s) =>
  s
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[.']/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
const words = (s) => normalize(s ?? '').split(' ').filter(Boolean);
const keys = new WeakMap();
function keyOf(s) {
  if (!keys.has(s)) {
    keys.set(s, { all: words(`${s.name} ${s.river} ${s.town ?? ''} ${s.region ?? ''} ${stateName(s.region) ?? ''} ${s.usgsId}`), town: words(s.town), river: words(s.river) });
  }
  return keys.get(s);
}

/**
 * Sites matching a query, best first. Every word typed must start a word of the site's river, town, gauge name or
 * state, or be part of its USGS number, so "del cal", "callicoon ny", "east br" and "014275" all work. Words of five
 * letters or more also match one typo away.
 */
export function searchSites(sites, query, near = null) {
  const q = words(query.replace(/^usgs-?/i, ''));
  if (!q.length) return near ? byDistance(sites, near) : sites;
  const scored = [];
  for (const s of sites) {
    let score = 0;
    for (const w of q) {
      const k = matchScore(s, w);
      if (!k) {
        score = 0;
        break;
      }
      score += k;
    }
    if (score) scored.push({ s, score: score + (s.inIndex ? 3 : 0) + (s.forecastable ? 2 : 0) });
  }
  scored.sort((a, b) => b.score - a.score || b.s.hasForecast - a.s.hasForecast || (b.s.areaMi2 ?? 0) - (a.s.areaMi2 ?? 0));
  return scored.map((x) => x.s);
}

function matchScore(s, w) {
  const num = s.usgsId;
  if (/^\d+$/.test(w)) return num === w ? 100 : num.startsWith(w) ? 60 : w.length >= 4 && num.includes(w) ? 30 : 0;
  const { all, town, river } = keyOf(s);
  if (town.includes(w)) return 50;
  if (town.some((t) => t.startsWith(w))) return 40;
  if (river.includes(w)) return 35;
  if (river.some((t) => t.startsWith(w))) return 25;
  if (w === s.region?.toLowerCase()) return 15;
  if (all.some((t) => t.startsWith(w))) return 10;
  return w.length >= 5 && [...town, ...river].some((t) => oneEditFrom(w, t.slice(0, w.length)) || oneEditFrom(w, t)) ? 5 : 0;
}

/** True when a and b differ by at most one inserted, deleted, substituted or swapped letter. */
function oneEditFrom(a, b) {
  if (Math.abs(a.length - b.length) > 1) return false;
  let i = 0;
  while (i < a.length && a[i] === b[i]) i++;
  const rest = (x, y) => a.slice(x) === b.slice(y);
  const swapped = a[i] === b[i + 1] && a[i + 1] === b[i] && rest(i + 2, i + 2);
  return swapped || rest(i + 1, i + 1) || rest(i + 1, i) || rest(i, i + 1);
}

export function byDistance(sites, from) {
  return [...sites].sort((a, b) => milesBetween(a, from) - milesBetween(b, from));
}

export function milesBetween(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 3958.8 * Math.asin(Math.sqrt(h));
}

// ---------------------------------------------------------------------------------------------- live data

/** A site's live.json (observations, status, pointer to the newest forecast), or null before its first forecast. */
export async function loadLive(id) {
  const r = await fetch(`/data/v1/sites/${id}/live.json`, { cache: 'no-cache' });
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`live.json: ${r.status}`);
  return r.json();
}

const forecasts = new Map();

/** A forecast file (forecast.schema.json). They're immutable, so each is fetched once. */
export function loadForecast(url) {
  if (!forecasts.has(url)) {
    forecasts.set(
      url,
      fetch(url).then((r) => {
        if (!r.ok) throw new Error(`forecast: ${r.status}`);
        return r.json();
      }),
    );
  }
  return forecasts.get(url);
}

// ---------------------------------------------------------------------------------------------- visits

/**
 * Whether opening a site should call POST /api/visit: always, unless it's always-on or already active for more than
 * a day. The call is idempotent.
 */
export function needsVisit(site, live = null, now = Date.now()) {
  if (site.alwaysOn || live?.always_on) return false;
  const until = live?.awake_until;
  return !((live?.status ?? site.status) === 'active' && until && Date.parse(until) - now > DAY);
}

/** POST /api/visit (api.schema.json#/$defs/visit): extends the active window and starts a wake run if needed. */
export async function visitSite(site) {
  const r = await fetch(`/api/visit?site=${encodeURIComponent(site.id)}`, { method: 'POST' });
  if (!r.ok) throw new Error(`visit: ${r.status}`);
  return r.json();
}

/** GET /api/status (api.schema.json#/$defs/status), polled while a site is waking. */
export async function siteStatus(site) {
  const r = await fetch(`/api/status?site=${encodeURIComponent(site.id)}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`status: ${r.status}`);
  return r.json();
}
