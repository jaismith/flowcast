// Every active USGS stream gauge, fetched by map view from the USGS Water Data API, and the rule for which of them
// flowcast can forecast. The backend will publish that rule; until then `eligibility` below is the only place it lives.

const USGS = 'https://api.waterdata.usgs.gov/ogcapi/v0/collections';
const DAY = 24 * 3600 * 1000;
const FLOW = '00060';
const WATER_TEMP = '00010';
/** A time series counts as reporting if its latest value is this recent (ice and outages leave multi-day gaps). */
const REPORTING_DAYS = 7;
/** Gauges with nothing at all in this long are left off the map. */
const ACTIVE_DAYS = 30;
const ID_BATCH = 150;
/** Gauges for an area are kept this long in localStorage; the keyless USGS API allows 1,000 requests an hour per IP. */
const CACHE_MS = 6 * 3600 * 1000;
const CACHE_PREFIX = 'flowcast:gauges:v1:';

/**
 * Whether flowcast can forecast a gauge, from the time series it reports. Both models forecast a change from the
 * latest reading, so flow needs live discharge and water temperature needs a live water thermometer.
 * Placeholder for the backend's rule (basin, drainage area, flow history and training region as well).
 */
export function eligibility(latest, now = Date.now()) {
  const reporting = (code) => latest[code] != null && now - latest[code] < REPORTING_DAYS * DAY;
  if (!reporting(FLOW)) {
    const since = latest[FLOW];
    return {
      forecastable: false,
      temperature: false,
      reason: since ? `Hasn’t reported flow since ${new Date(since).toLocaleDateString('en-US', { month: 'short', year: 'numeric' })}` : 'Doesn’t measure flow',
    };
  }
  return { forecastable: true, temperature: reporting(WATER_TEMP), reason: null };
}

// ---------------------------------------------------------------------------------------------- by map view

const known = new Map();
const fetched = [];
const inflight = new Map();

/** Every gauge fetched so far, by id. */
export const knownGauges = () => known;

/**
 * Active stream gauges in a bounding box [w, s, e, n], snapped outward to a half-degree grid so small pans reuse
 * the last request. Resolves once they are in `knownGauges()`.
 */
export function loadGaugesIn(bbox) {
  const snapped = [snap(bbox[0], Math.floor), snap(bbox[1], Math.floor), snap(bbox[2], Math.ceil), snap(bbox[3], Math.ceil)];
  if (fetched.some((b) => contains(b, snapped))) return Promise.resolve();
  const key = snapped.join(',');
  const cached = readCache(key);
  if (cached) {
    for (const g of cached) if (!known.has(g.id)) known.set(g.id, g);
    fetched.push(snapped);
    return Promise.resolve();
  }
  if (!inflight.has(key)) {
    inflight.set(
      key,
      fetchGauges(`bbox=${key}`)
        .then((ids) => {
          fetched.push(snapped);
          const [w, s, e, n] = snapped;
          writeCache(key, [...known.values()].filter((g) => g.lon >= w && g.lon <= e && g.lat >= s && g.lat <= n));
          return ids;
        })
        .finally(() => inflight.delete(key)),
    );
  }
  return inflight.get(key);
}

function readCache(key) {
  try {
    const hit = JSON.parse(localStorage.getItem(CACHE_PREFIX + key));
    return hit && Date.now() - hit.at < CACHE_MS ? hit.gauges : null;
  } catch {
    return null;
  }
}

function writeCache(key, gauges) {
  const value = JSON.stringify({ at: Date.now(), gauges });
  for (let tries = 0; tries < 2; tries++) {
    try {
      localStorage.setItem(CACHE_PREFIX + key, value);
      return;
    } catch {
      // full: drop every cached area and try once more
      for (const k of Object.keys(localStorage)) if (k.startsWith(CACHE_PREFIX)) localStorage.removeItem(k);
    }
  }
}

/** One gauge by USGS id ("USGS-01427510"), or null if USGS has no stream gauge by that id. */
export async function lookupGauge(id) {
  if (known.has(id)) return known.get(id);
  await fetchGauges(`monitoring_location_id=${id}`, { keepInactive: true });
  return known.get(id) ?? null;
}

async function fetchGauges(where, { keepInactive = false } = {}) {
  const latest = await getJSON(`${USGS}/latest-continuous/items?${where}&f=json&limit=10000&skipGeometry=true&properties=monitoring_location_id,parameter_code,time`);
  const series = new Map();
  for (const f of latest.features ?? []) {
    const { monitoring_location_id: id, parameter_code: code, time } = f.properties;
    if (!series.has(id)) series.set(id, {});
    series.get(id)[code] = Date.parse(time);
  }
  const now = Date.now();
  const ids = [...series.keys()].filter((id) => !known.has(id) && (keepInactive || Object.values(series.get(id)).some((t) => now - t < ACTIVE_DAYS * DAY)));
  for (let i = 0; i < ids.length; i += ID_BATCH) {
    const batch = ids.slice(i, i + ID_BATCH);
    const locs = await getJSON(
      `${USGS}/monitoring-locations/items?f=json&limit=${ID_BATCH}&site_type_code=ST&properties=monitoring_location_name,state_name,drainage_area&id=${batch.join(',')}`,
    );
    for (const f of locs.features ?? []) known.set(f.id, gaugeSite(f, series.get(f.id)));
  }
}

async function getJSON(url) {
  const r = await fetch(url);
  if (r.status === 429) throw Object.assign(new Error('USGS is limiting requests from this network for a few minutes'), { status: 429 });
  if (!r.ok) throw Object.assign(new Error(`USGS answered ${r.status}`), { status: r.status });
  return r.json();
}

const snap = (v, round) => round(v * 2) / 2;
const contains = (a, b) => a[0] <= b[0] && a[1] <= b[1] && a[2] >= b[2] && a[3] >= b[3];

// ---------------------------------------------------------------------------------------------- names

function gaugeSite(f, latest) {
  const [lon, lat] = f.geometry.coordinates;
  const { river, town, region } = parseName(f.properties.monitoring_location_name, f.properties.state_name);
  const e = eligibility(latest);
  return {
    id: f.id,
    usgsId: f.id.replace(/^USGS-/, ''),
    slug: null,
    name: `${river}${town ? ` at ${town}` : ''}${region ? `, ${region}` : ''}`,
    river,
    town,
    region,
    lat,
    lon,
    areaMi2: f.properties.drainage_area ?? null,
    inIndex: false,
    hasForecast: false,
    lifecycle: null,
    alwaysOn: false,
    forecastable: e.forecastable,
    temperature: e.temperature,
    reason: e.reason,
  };
}

const WORDS = {
  R: 'River', RV: 'River', RIV: 'River', CR: 'Creek', CK: 'Creek', CRK: 'Creek', BR: 'Branch', BRN: 'Branch', FK: 'Fork', TRIB: 'Tributary',
  TR: 'Tributary', MT: 'Mount', MTN: 'Mountain', FT: 'Fort', PT: 'Point', ST: 'St.', STA: 'Station', LK: 'Lake', RES: 'Reservoir',
  SPGS: 'Springs', HTS: 'Heights', JCT: 'Junction', NR: 'near', AB: 'above', ABV: 'above', BL: 'below', BLW: 'below', AT: 'at', NEAR: 'near',
  ABOVE: 'above', BELOW: 'below', 'NO.': 'No.', N: 'North', S: 'South', E: 'East', W: 'West', F: 'Fork', L: 'Little', LT: 'Little',
};
const SMALL = new Set(['at', 'near', 'above', 'below', 'and', 'of', 'the', 'in', 'on', 'from']);
const CONNECTORS = ['at', 'near', 'above', 'below'];

/** "N F SOUTH BR POTOMAC R AT CABINS, WV" → { river: "North Fork South Branch Potomac River", town: "Cabins", region: "WV" }. */
export function parseName(raw, stateName) {
  const region = STATE_CODES[stateName?.toLowerCase()] ?? null;
  let words = raw.replace(/,/g, ' , ').split(/\s+/).filter(Boolean);
  // A trailing two-letter state ("... CALLICOON NY") names the town's state, which can differ from the gauge's bank.
  let townState = null;
  const last = words.at(-1);
  if (/^[A-Z]{2}\.?$/.test(last) && VALID_CODES.has(last.replace('.', ''))) {
    townState = last.replace('.', '');
    words = words.slice(0, -1);
  }
  words = words.filter((w) => w !== ',');
  const title = words.map((w, i) => {
    const up = w.toUpperCase();
    const mapped = WORDS[up];
    if (mapped) return i === 0 && SMALL.has(mapped) ? mapped[0].toUpperCase() + mapped.slice(1) : mapped;
    if (/^\d/.test(w)) return w.toLowerCase();
    return w.length && SMALL.has(w.toLowerCase()) && i > 0 ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase();
  });
  const k = title.findIndex((w, i) => i > 0 && CONNECTORS.includes(w));
  const river = (k > 0 ? title.slice(0, k) : title).join(' ');
  const town = k > 0 ? title.slice(k + 1).join(' ') : null;
  return { river, town, region: townState ?? region };
}

const STATE_CODES = Object.fromEntries(
  'AL alabama|AK alaska|AZ arizona|AR arkansas|CA california|CO colorado|CT connecticut|DE delaware|DC district of columbia|FL florida|GA georgia|HI hawaii|ID idaho|IL illinois|IN indiana|IA iowa|KS kansas|KY kentucky|LA louisiana|ME maine|MD maryland|MA massachusetts|MI michigan|MN minnesota|MS mississippi|MO missouri|MT montana|NE nebraska|NV nevada|NH new hampshire|NJ new jersey|NM new mexico|NY new york|NC north carolina|ND north dakota|OH ohio|OK oklahoma|OR oregon|PA pennsylvania|RI rhode island|SC south carolina|SD south dakota|TN tennessee|TX texas|UT utah|VT vermont|VA virginia|WA washington|WV west virginia|WI wisconsin|WY wyoming|PR puerto rico'
    .split('|')
    .map((s) => [s.slice(3), s.slice(0, 2)]),
);
const VALID_CODES = new Set(Object.values(STATE_CODES));
export const stateName = (code) => Object.keys(STATE_CODES).find((k) => STATE_CODES[k] === code) ?? null;
