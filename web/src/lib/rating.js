import { C } from './palette.js';

const ratings = Object.fromEntries(
  Object.values(import.meta.glob('../data/rating-*.json', { eager: true, import: 'default' })).map((r) => [r.site, r]),
);

function interp(xs, ys, x) {
  if (x <= xs[0]) return ys[0];
  if (x >= xs.at(-1)) return ys.at(-1);
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid;
    else hi = mid;
  }
  return ys[lo] + ((ys[hi] - ys[lo]) * (x - xs[lo])) / (xs[hi] - xs[lo]);
}

/** Today's USGS rating for a site, or null. Applying it to past flows is approximate (ratings shift). */
export function ratingFor(site) {
  const r = ratings[site];
  if (!r) return null;
  return {
    id: r.rating_id,
    stage: (cfs) => (cfs == null ? null : interp(r.flow_cfs, r.stage_ft, cfs)),
    flow: (ft) => interp(r.stage_ft, r.flow_cfs, ft),
  };
}

const LEVELS = [
  { key: 'action', label: 'Action', color: C.action },
  { key: 'minor', label: 'Minor flood', color: C.minor },
  { key: 'moderate', label: 'Moderate flood', color: C.moderate },
  { key: 'major', label: 'Major flood', color: C.major },
];

/** NWS flood categories for the site, with the flow at each stage when a rating is available. */
export function floodLevels(meta) {
  const fs = meta.flood_stage_ft;
  if (!fs) return [];
  const rating = ratingFor(meta.id);
  return LEVELS.filter((l) => fs[l.key] != null).map((l) => ({ ...l, ft: fs[l.key], cfs: rating?.flow(fs[l.key]) ?? null }));
}

/** The highest category a stage has reached, or null below action. */
export function categoryAt(levels, ft) {
  if (ft == null) return null;
  return [...levels].reverse().find((l) => ft >= l.ft) ?? null;
}
