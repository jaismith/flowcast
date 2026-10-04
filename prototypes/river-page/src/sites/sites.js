import seedUrl from './sites.json?url';
import { stateName } from './gauges.js';

// The serving contract (serving/schema/ on cursor/production-backend-30da): the index at /data/v1/sites.json and
// POST /api/visit, GET /api/status on the same CloudFront domain. Set VITE_FLOWCAST_ORIGIN to use it ('' for the
// page's own origin); until then the index is the seed file next to this one and wake-ups are simulated.
const ORIGIN = import.meta.env.VITE_FLOWCAST_ORIGIN?.replace(/\/$/, '');
const LIVE = ORIGIN != null;
const SITES_URL = LIVE ? `${ORIGIN}/data/v1/sites.json` : seedUrl;

export const DEFAULT_SITE = 'USGS-01427510';
const LAST_SITE_KEY = 'flowcast:last-site';
const DAY = 24 * 3600 * 1000;
const SIM_ETA_S = 120;

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
 * Every site in the flowcast index (sites.schema.json), as the same site objects gauges.js makes for other USGS
 * gauges. Fetched once per page load.
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
  const m = /^(.*?) (?:at|near|above|below) (.*?)(?:, ([A-Z]{2}))?$/.exec(e.name);
  return {
    id: `USGS-${e.usgs_id}`,
    usgsId: e.usgs_id,
    slug: e.site,
    name: e.name,
    river: m?.[1] ?? e.name,
    town: e.short_name ?? m?.[2] ?? null,
    region: m?.[3] ?? null,
    lat: e.lat,
    lon: e.lon,
    areaMi2: e.drainage_area_sq_mi ?? null,
    inIndex: true,
    hasForecast: e.forecast_issue_time != null,
    lifecycle: e.state,
    alwaysOn: !!e.always_on,
    forecastable: true,
    temperature: !!e.has_temperature,
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

// ---------------------------------------------------------------------------------------------- visits

/**
 * Whether opening a site should call POST /api/visit: always, unless it's always-on or already active for more than a
 * day. The index has no `awake_until`, so a visit-driven active site is visited again; the call is idempotent.
 */
export function needsVisit(site, awakeUntil = null, now = Date.now()) {
  if (site.alwaysOn) return false;
  return !(site.lifecycle === 'active' && awakeUntil && Date.parse(awakeUntil) - now > DAY);
}

/** POST /api/visit (api.schema.json#/$defs/visit): extends the active window and starts a wake run if needed. */
export async function visitSite(site) {
  if (!LIVE) return simulated(site);
  const r = await fetch(`${ORIGIN}/api/visit?site=${encodeURIComponent(apiKey(site))}`, { method: 'POST' });
  if (!r.ok) throw new Error(`visit: ${r.status}`);
  return r.json();
}

/** GET /api/status (api.schema.json#/$defs/status), polled while a site is waking. */
export async function siteStatus(site) {
  if (!LIVE) return simulated(site);
  const r = await fetch(`${ORIGIN}/api/status?site=${encodeURIComponent(apiKey(site))}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`status: ${r.status}`);
  return r.json();
}

// Gauges outside the index have no slug yet; the backend has to accept their USGS id.
const apiKey = (site) => site.slug ?? site.id;

// The prototype has no model to run: a site without a forecast wakes forever (the clock survives reloads), so the
// page shows the warming loader and, after the polling limit, the paused message.
function simulated(site) {
  const key = `flowcast:wake:${site.id}`;
  let started = Number(sessionStorage.getItem(key));
  if (!started) {
    started = Date.now();
    sessionStorage.setItem(key, String(started));
  }
  const awake_until = new Date(Date.now() + 7 * DAY).toISOString();
  if (site.hasForecast) return { site: apiKey(site), state: 'active', awake_until, forecast: null, eta_s: null };
  return {
    site: apiKey(site),
    state: 'waking',
    awake_until,
    forecast: null,
    eta_s: Math.max(0, Math.round(SIM_ETA_S - (Date.now() - started) / 1000)),
    run: { status: 'running', started: new Date(started).toISOString(), finished: null },
  };
}
