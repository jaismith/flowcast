import { C } from './palette.js';

const LEVELS = {
  action: { label: 'Action', color: C.action },
  minor: { label: 'Minor flood', color: C.minor },
  moderate: { label: 'Moderate flood', color: C.moderate },
  major: { label: 'Major flood', color: C.major },
};

/** NWS flood categories from static.json, lowest first, with the flow at each stage when the backend gives it. */
export function floodLevels(floods) {
  return floods
    .filter((f) => LEVELS[f.category])
    .map((f) => ({ key: f.category, ...LEVELS[f.category], ft: f.stage_ft, cfs: f.flow_cfs ?? null }))
    .sort((a, b) => a.ft - b.ft);
}

/** The highest category a stage has reached, or null below action. */
export function categoryAt(levels, ft) {
  if (ft == null) return null;
  return [...levels].reverse().find((l) => ft >= l.ft) ?? null;
}

/** The highest category a flow reaches, for categories with a flow, or null. */
export function categoryAtFlow(levels, cfs) {
  if (cfs == null) return null;
  return [...levels].reverse().find((l) => l.cfs != null && cfs >= l.cfs) ?? null;
}
