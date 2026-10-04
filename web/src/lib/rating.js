import { C } from './palette.js';

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

/** Stage/flow conversions from the bundle's current USGS rating, or null if the gauge has none. */
export function ratingFrom(r) {
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
export function floodLevels(site, rating) {
  const fs = site.flood_stage_ft;
  if (!fs) return [];
  return LEVELS.filter((l) => fs[l.key] != null).map((l) => ({ ...l, ft: fs[l.key], cfs: rating?.flow(fs[l.key]) ?? null }));
}

/** The highest category a stage has reached, or null below action. */
export function categoryAt(levels, ft) {
  if (ft == null) return null;
  return [...levels].reverse().find((l) => ft >= l.ft) ?? null;
}
