// The national gauge catalog (gauges.schema.json): every lower-48 USGS discharge gauge in 5° tiles, each with the
// backend's eligibility verdict. The page never calls USGS; the backend rebuilds the catalog daily.

const INDEX_URL = '/data/v1/gauges/index.json';
const KM2_PER_MI2 = 2.58999;

let indexPromise = null;
const tiles = new Map();
const known = new Map();

/** The catalog index: tile keys, rule thresholds and the text for each reason code. Fetched once per page load. */
export function loadCatalog() {
  indexPromise ??= fetch(INDEX_URL)
    .then((r) => {
      if (!r.ok) throw new Error(`gauge catalog: ${r.status}`);
      return r.json();
    })
    .catch((e) => {
      indexPromise = null;
      throw e;
    });
  return indexPromise;
}

/** Every catalog gauge loaded so far, by id. */
export const knownGauges = () => known;

/** Loads the catalog tiles that intersect [w, s, e, n]; resolves once their gauges are in `knownGauges()`. */
export async function loadGaugesIn([w, s, e, n]) {
  const index = await loadCatalog();
  const step = index.tile_deg;
  const keys = [];
  for (let x = Math.floor(w / step) * step; x < e; x += step) {
    for (let y = Math.floor(s / step) * step; y < n; y += step) {
      const key = `${x}_${y}`;
      if (key in index.tiles) keys.push(key);
    }
  }
  await Promise.all(keys.map((key) => loadTile(index, key)));
}

function loadTile(index, key) {
  if (!tiles.has(key)) {
    const p = fetch(index.tile_url.replace('{key}', key))
      .then((r) => {
        if (!r.ok) throw new Error(`gauge tile ${key}: ${r.status}`);
        return r.json();
      })
      .then((tile) => {
        for (const g of tile.gauges) if (!known.has(g.id)) known.set(g.id, gaugeSite(g, index.rule));
      })
      .catch((e) => {
        tiles.delete(key);
        throw e;
      });
    tiles.set(key, p);
  }
  return tiles.get(key);
}

/** A catalog gauge by id, if its tile has been loaded (the catalog can't be searched by id without a location). */
export const lookupGauge = (id) => known.get(id) ?? null;

/**
 * What the page says about a catalog gauge, from the backend's verdict. `model_basin` gauges can be forecast now;
 * `eligible` ones pass every check but wait for on-the-fly onboarding; `ineligible` ones carry reason codes.
 */
export function eligibility(g, rule) {
  const { status, forecast_now, reasons } = g.eligibility;
  if (forecast_now) return { forecastable: true, temperature: g.has_temp, reason: null };
  const text = reasons.map((code) => rule.reasons?.[code] ?? code.replaceAll('_', ' '));
  const reason = status === 'eligible' ? 'not one of the basins flowcast forecasts yet' : text.join('; ');
  return { forecastable: false, temperature: false, reason: reason[0].toUpperCase() + reason.slice(1) };
}

function gaugeSite(g, rule) {
  const { river, town, region } = parseName(g.name);
  const e = eligibility(g, rule);
  return {
    id: g.id,
    usgsId: g.id.replace(/^USGS-/, ''),
    slug: null,
    name: `${river}${town ? ` at ${town}` : ''}${region ? `, ${region}` : ''}`,
    river,
    town,
    region,
    lat: g.lat,
    lon: g.lon,
    areaMi2: g.area_km2 == null ? null : g.area_km2 / KM2_PER_MI2,
    inIndex: false,
    hasForecast: false,
    status: null,
    alwaysOn: false,
    inTrainingRegion: g.in_training_region,
    forecastable: e.forecastable,
    temperature: e.temperature,
    reason: e.reason,
  };
}

// ---------------------------------------------------------------------------------------------- names

const WORDS = {
  R: 'River', RV: 'River', RIV: 'River', CR: 'Creek', CK: 'Creek', CRK: 'Creek', BR: 'Branch', BRN: 'Branch', FK: 'Fork', TRIB: 'Tributary',
  TR: 'Tributary', MT: 'Mount', MTN: 'Mountain', FT: 'Fort', PT: 'Point', ST: 'St.', STA: 'Station', LK: 'Lake', RES: 'Reservoir',
  SPGS: 'Springs', HTS: 'Heights', JCT: 'Junction', NR: 'near', AB: 'above', ABV: 'above', BL: 'below', BLW: 'below', AT: 'at', NEAR: 'near',
  ABOVE: 'above', BELOW: 'below', 'NO.': 'No.', N: 'North', S: 'South', E: 'East', W: 'West', F: 'Fork', L: 'Little', LT: 'Little',
};
const SMALL = new Set(['at', 'near', 'above', 'below', 'and', 'of', 'the', 'in', 'on', 'from']);
const CONNECTORS = ['at', 'near', 'above', 'below'];

/** "N F SOUTH BR POTOMAC R AT CABINS, WV" → { river: "North Fork South Branch Potomac River", town: "Cabins", region: "WV" }. */
export function parseName(raw) {
  let words = raw.replace(/,/g, ' , ').split(/\s+/).filter(Boolean);
  let region = null;
  const last = words.at(-1)?.replace('.', '');
  if (/^[A-Z]{2}$/.test(last ?? '') && VALID_CODES.has(last)) {
    region = last;
    words = words.slice(0, -1);
  }
  words = words.filter((w) => w !== ',');
  const title = words.map((w, i) => {
    const mapped = WORDS[w.toUpperCase()];
    if (mapped) return i === 0 && SMALL.has(mapped) ? mapped[0].toUpperCase() + mapped.slice(1) : mapped;
    if (/^\d/.test(w)) return w.toLowerCase();
    return SMALL.has(w.toLowerCase()) && i > 0 ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase();
  });
  const k = title.findIndex((w, i) => i > 0 && CONNECTORS.includes(w));
  const river = (k > 0 ? title.slice(0, k) : title).join(' ');
  const town = k > 0 ? title.slice(k + 1).join(' ') : null;
  return { river, town, region };
}

const STATE_CODES = Object.fromEntries(
  'AL alabama|AK alaska|AZ arizona|AR arkansas|CA california|CO colorado|CT connecticut|DE delaware|DC district of columbia|FL florida|GA georgia|HI hawaii|ID idaho|IL illinois|IN indiana|IA iowa|KS kansas|KY kentucky|LA louisiana|ME maine|MD maryland|MA massachusetts|MI michigan|MN minnesota|MS mississippi|MO missouri|MT montana|NE nebraska|NV nevada|NH new hampshire|NJ new jersey|NM new mexico|NY new york|NC north carolina|ND north dakota|OH ohio|OK oklahoma|OR oregon|PA pennsylvania|RI rhode island|SC south carolina|SD south dakota|TN tennessee|TX texas|UT utah|VT vermont|VA virginia|WA washington|WV west virginia|WI wisconsin|WY wyoming|PR puerto rico'
    .split('|')
    .map((s) => [s.slice(3), s.slice(0, 2)]),
);
const VALID_CODES = new Set(Object.values(STATE_CODES));
export const stateName = (code) => Object.keys(STATE_CODES).find((k) => STATE_CODES[k] === code) ?? null;
