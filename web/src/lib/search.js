import { stateName } from './places.ts';

/** "Callicoon, NY" for a Place. */
export const placeOf = (s) => [s.town, s.region].filter(Boolean).join(', ');
export const titleOf = (s) => (s.town ? `${s.river} at ${s.town}` : s.river);

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
 * Places matching a query, best first. Every word typed must start a word of the site's river, town, gauge name or
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
