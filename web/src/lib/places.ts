import type { Gauge, GaugeIds, GaugeIndex, ReasonCode, SiteId, SiteIndex, SiteSummary, Status } from './contract';
import { loadGaugeIds, loadGaugeIndex, loadGaugeTile, loadSiteIndex } from './site.ts';

/**
 * One gauge as the site selector lists and maps it: a sites.json site (`inIndex`) or a national-catalog gauge the
 * map loaded. Only index sites can be opened as forecast pages; catalog gauges outside the index can't be forecast.
 */
export interface Place {
  id: SiteId;
  /** "01427510" */
  usgsId: string;
  slug: string | null;
  name: string;
  river: string;
  town: string | null;
  /** Two-letter state code. */
  region: string | null;
  lat: number;
  lon: number;
  areaMi2: number | null;
  inIndex: boolean;
  /** A forecast is published (drawn solid on the map). */
  hasForecast: boolean;
  status: Status | null;
  alwaysOn: boolean;
  forecastable: boolean;
  /** Water temperature is forecast too. */
  temperature: boolean;
  /** Why it can't be forecast, as a sentence fragment; null when forecastable. */
  reason: string | null;
  /** The sites.json entry, for index sites. */
  summary: SiteSummary | null;
}

const KM2_PER_MI2 = 2.58999;

// ---------------------------------------------------------------------------------------------- the index

let catalogPromise: Promise<GaugeIndex> | null = null;

/** The catalog index: tile keys, the rule's thresholds and the text for each reason code. Fetched once per page load. */
export function loadCatalog(): Promise<GaugeIndex> {
  catalogPromise ??= loadGaugeIndex().catch((e) => {
    catalogPromise = null;
    throw e;
  });
  return catalogPromise;
}

let sitesPromise: Promise<{ places: Place[]; defaultId: SiteId }> | null = null;

/** Every sites.json site as a Place, and the site served at `/`. The catalog index supplies reason text. */
export function loadPlaces(): Promise<{ places: Place[]; defaultId: SiteId }> {
  const rule = loadCatalog().then(
    (index) => index.rule,
    () => null,
  );
  sitesPromise ??= Promise.all([loadSiteIndex(), rule])
    .then(([idx, rule]: [SiteIndex, GaugeIndex['rule'] | null]) => ({ places: idx.sites.map((s) => fromSummary(s, rule)), defaultId: idx.default }))
    .catch((e) => {
      sitesPromise = null;
      throw e;
    });
  return sitesPromise;
}

function fromSummary(s: SiteSummary, rule: GaugeIndex['rule'] | null): Place {
  const forecastable = s.forecastable !== false;
  return {
    id: s.id,
    usgsId: usgsNumber(s.id),
    slug: s.slug ?? null,
    name: s.name,
    river: s.river,
    town: s.town,
    region: s.state,
    lat: s.lat,
    lon: s.lon,
    areaMi2: s.area_mi2,
    inIndex: true,
    hasForecast: s.forecast_ready,
    status: s.status ?? null,
    alwaysOn: !!s.always_on,
    forecastable,
    temperature: forecastable && !!s.has_temp,
    reason: forecastable ? null : reasonText([s.not_forecastable_reason ?? 'not_forecastable'], rule),
    summary: s,
  };
}

export const usgsNumber = (id: string) => id.replace(/^USGS-/, '');

/** The rule's text for reason codes ("short_record" → "Fewer than 10 years of daily discharge"). */
export function reasonText(codes: (ReasonCode | string)[], rule: GaugeIndex['rule'] | null): string {
  const reasons = (rule?.reasons ?? {}) as Record<string, string | undefined>;
  const text = codes.map((code) => reasons[code] ?? code.replaceAll('_', ' ')).join('; ');
  return text && text[0].toUpperCase() + text.slice(1);
}

// ---------------------------------------------------------------------------------------------- catalog tiles

let idsPromise: Promise<GaugeIds> | null = null;
const tiles = new Map<string, Promise<void>>();
const known = new Map<SiteId, Place>();

/** Every catalog gauge loaded so far, by id. */
export const knownGauges = () => known;

/** A catalog gauge by id, if its tile has been loaded. */
export const lookupGauge = (id: SiteId): Place | null => known.get(id) ?? null;

/** Loads the catalog tiles that intersect [w, s, e, n]; resolves once their gauges are in `knownGauges()`. */
export async function loadGaugesIn([w, s, e, n]: number[]): Promise<void> {
  const index = await loadCatalog();
  const step = index.tile_deg;
  const keys: string[] = [];
  for (let x = Math.floor(w / step) * step; x < e; x += step) {
    for (let y = Math.floor(s / step) * step; y < n; y += step) {
      const key = `${x}_${y}`;
      if (key in index.tiles) keys.push(key);
    }
  }
  await Promise.all(keys.map((key) => loadTile(index, key)));
}

function loadTile(index: GaugeIndex, key: string): Promise<void> {
  if (!tiles.has(key)) {
    const p = loadGaugeTile(index, key)
      .then((tile) => {
        for (const g of tile.gauges) if (!known.has(g.id)) known.set(g.id, gaugePlace(g, index.rule));
      })
      .catch((e) => {
        tiles.delete(key);
        throw e;
      });
    tiles.set(key, p);
  }
  return tiles.get(key)!;
}

/** A catalog gauge by id, loading its tile through ids.json (fetched only for a direct link). Null if the catalog has no such gauge. */
export async function findGauge(id: SiteId): Promise<Place | null> {
  if (known.has(id)) return known.get(id)!;
  const index = await loadCatalog();
  idsPromise ??= loadGaugeIds(index).catch((e) => {
    idsPromise = null;
    throw e;
  });
  const key = (await idsPromise).tiles[id];
  if (!key) return null;
  await loadTile(index, key);
  return known.get(id) ?? null;
}

/**
 * What the page says about a catalog gauge, from the backend's verdict. `model_basin` gauges can be forecast now;
 * `eligible` ones pass every check but wait for on-the-fly onboarding; `ineligible` ones carry reason codes.
 */
function eligibility(g: Gauge, rule: GaugeIndex['rule']) {
  const { status, forecast_now, reasons } = g.eligibility;
  if (forecast_now) return { forecastable: true, temperature: g.has_temp, reason: null };
  const reason = status === 'eligible' ? 'Not one of the basins flowcast forecasts yet' : reasonText(reasons, rule);
  return { forecastable: false, temperature: false, reason };
}

function gaugePlace(g: Gauge, rule: GaugeIndex['rule']): Place {
  const { river, town, region } = parseName(g.name);
  return {
    id: g.id,
    usgsId: usgsNumber(g.id),
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
    ...eligibility(g, rule),
    summary: null,
  };
}

// ---------------------------------------------------------------------------------------------- names

const WORDS: Record<string, string> = {
  R: 'River', RV: 'River', RIV: 'River', CR: 'Creek', CK: 'Creek', CRK: 'Creek', BR: 'Branch', BRN: 'Branch', FK: 'Fork', TRIB: 'Tributary',
  TR: 'Tributary', MT: 'Mount', MTN: 'Mountain', FT: 'Fort', PT: 'Point', ST: 'St.', STA: 'Station', LK: 'Lake', RES: 'Reservoir',
  SPGS: 'Springs', HTS: 'Heights', JCT: 'Junction', NR: 'near', AB: 'above', ABV: 'above', BL: 'below', BLW: 'below', AT: 'at', NEAR: 'near',
  ABOVE: 'above', BELOW: 'below', 'NO.': 'No.', N: 'North', S: 'South', E: 'East', W: 'West', F: 'Fork', L: 'Little', LT: 'Little',
};
const SMALL = new Set(['at', 'near', 'above', 'below', 'and', 'of', 'the', 'in', 'on', 'from']);
const CONNECTORS = ['at', 'near', 'above', 'below'];

/** "N F SOUTH BR POTOMAC R AT CABINS, WV" → { river: "North Fork South Branch Potomac River", town: "Cabins", region: "WV" }. */
export function parseName(raw: string): { river: string; town: string | null; region: string | null } {
  let words = raw.replace(/,/g, ' , ').split(/\s+/).filter(Boolean);
  let region: string | null = null;
  const last = words.at(-1)?.replace('.', '') ?? '';
  if (/^[A-Z]{2}$/.test(last) && VALID_CODES.has(last)) {
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

const STATE_CODES: Record<string, string> = Object.fromEntries(
  'AL alabama|AK alaska|AZ arizona|AR arkansas|CA california|CO colorado|CT connecticut|DE delaware|DC district of columbia|FL florida|GA georgia|HI hawaii|ID idaho|IL illinois|IN indiana|IA iowa|KS kansas|KY kentucky|LA louisiana|ME maine|MD maryland|MA massachusetts|MI michigan|MN minnesota|MS mississippi|MO missouri|MT montana|NE nebraska|NV nevada|NH new hampshire|NJ new jersey|NM new mexico|NY new york|NC north carolina|ND north dakota|OH ohio|OK oklahoma|OR oregon|PA pennsylvania|RI rhode island|SC south carolina|SD south dakota|TN tennessee|TX texas|UT utah|VT vermont|VA virginia|WA washington|WV west virginia|WI wisconsin|WY wyoming|PR puerto rico'
    .split('|')
    .map((s) => [s.slice(3), s.slice(0, 2)]),
);
const VALID_CODES = new Set(Object.values(STATE_CODES));
export const stateName = (code: string | null) => Object.keys(STATE_CODES).find((k) => STATE_CODES[k] === code) ?? null;
