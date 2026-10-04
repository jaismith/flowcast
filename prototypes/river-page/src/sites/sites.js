import seedUrl from './sites.json?url';

// The sites index and the forecast wake-up both come from the flowcast API once VITE_FLOWCAST_API is set.
// Until then the index is the seed list next to this file and wake-ups are simulated.
const API = import.meta.env.VITE_FLOWCAST_API?.replace(/\/$/, '') ?? null;
const SITES_URL = import.meta.env.VITE_SITES_URL ?? (API ? `${API}/sites.json` : seedUrl);

export const DEFAULT_SITE = 'USGS-01427510';
const LAST_SITE_KEY = 'flowcast:last-site';
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

/** The site a URL asks for: `/site/<id>`, or the older `?site=<number>`. Unrecognized ids are returned as typed. */
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

/** Every site flowcast covers, with a precomputed search key. Fetched once per page load. */
export function loadSites() {
  sitesPromise ??= fetch(SITES_URL)
    .then((r) => {
      if (!r.ok) throw new Error(`sites index: ${r.status}`);
      return r.json();
    })
    .then((j) => j.sites.map((s) => ({ ...s, place: `${s.town}, ${s.state}`, words: words(searchText(s)) })))
    .catch((e) => {
      sitesPromise = null;
      throw e;
    });
  return sitesPromise;
}

const STATES = {
  CT: 'connecticut', DE: 'delaware', KY: 'kentucky', MA: 'massachusetts', MD: 'maryland', ME: 'maine', MN: 'minnesota',
  NC: 'north carolina', NH: 'new hampshire', NJ: 'new jersey', NY: 'new york', OH: 'ohio', PA: 'pennsylvania',
  RI: 'rhode island', TN: 'tennessee', VA: 'virginia', VT: 'vermont', WI: 'wisconsin', WV: 'west virginia',
};

const normalize = (s) =>
  s
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
const words = (s) => normalize(s).split(' ').filter(Boolean);
const searchText = (s) => `${s.name} ${s.river} ${s.town} ${s.state} ${STATES[s.state] ?? ''} ${usgsNumber(s.id)}`;

/**
 * Sites matching a query, best first. Every word typed must start a word of the site's river, town, gauge name or
 * state, or be part of its USGS number, so "del cal", "callicoon ny", "east br" and "014275" all work.
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
    if (score) scored.push({ s, score });
  }
  scored.sort((a, b) => b.score - a.score || b.s.forecast_ready - a.s.forecast_ready || b.s.area_mi2 - a.s.area_mi2);
  return scored.map((x) => x.s);
}

function matchScore(s, w) {
  const num = usgsNumber(s.id);
  if (/^\d+$/.test(w)) return num === w ? 100 : num.startsWith(w) ? 60 : w.length >= 4 && num.includes(w) ? 30 : 0;
  const town = words(s.town);
  const river = words(s.river);
  if (town.includes(w)) return 50;
  if (town.some((t) => t.startsWith(w))) return 40;
  if (river.includes(w)) return 35;
  if (river.some((t) => t.startsWith(w))) return 25;
  if (w === s.state.toLowerCase()) return 15;
  return s.words.some((t) => t.startsWith(w)) ? 10 : 0;
}

export function byDistance(sites, from) {
  const d = (s) => (s.lat - from.lat) ** 2 + ((s.lon - from.lon) * Math.cos((from.lat * Math.PI) / 180)) ** 2;
  return [...sites].sort((a, b) => d(a) - d(b));
}

// ---------------------------------------------------------------------------------------------- lazy forecasts

/**
 * Asks for a forecast for a site that has no fresh one; the backend starts a run and says roughly how long it
 * will take. Safe to call on every visit. Resolves to { forecast_ready, eta_s, started_at }.
 */
export async function wakeForecast(id) {
  if (API) {
    const r = await fetch(`${API}/sites/${id}/wake`, { method: 'POST' });
    if (!r.ok) throw new Error(`wake ${id}: ${r.status}`);
    return r.json();
  }
  return simulatedWake(id);
}

/** The site's current index entry, polled while its forecast warms up. */
export async function forecastStatus(id) {
  if (API) {
    const r = await fetch(`${API}/sites/${id}`);
    if (!r.ok) throw new Error(`status ${id}: ${r.status}`);
    return r.json();
  }
  return simulatedWake(id);
}

// The prototype has no model to run, so a simulated wake-up starts a clock (kept across reloads) and never finishes.
function simulatedWake(id) {
  const key = `flowcast:wake:${id}`;
  let started = Number(sessionStorage.getItem(key));
  if (!started) {
    started = Date.now();
    sessionStorage.setItem(key, String(started));
  }
  return { forecast_ready: false, eta_s: SIM_ETA_S, started_at: new Date(started).toISOString() };
}
