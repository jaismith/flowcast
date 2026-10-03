import { categoryAt } from './rating.js';
import { flowClass, fmt, normalFor } from './data.js';
import { C } from './palette.js';

const HOUR = 3600 * 1000;

const CLASS_COLOR = { low: () => C.sun, normal: () => C.melt, high: () => C.rain };

export const partOfDay = (d) => {
  const h = Number(d.toLocaleString('en-US', { hour: 'numeric', hour12: false, timeZone: 'America/New_York' }));
  const wd = d.toLocaleDateString('en-US', { weekday: 'short', timeZone: 'America/New_York' });
  return `${wd} ${h < 5 ? 'night' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening'}`;
};

/** The river's state in a fixed vocabulary, so the header reads the same way on every visit. */
export function riverStatus({ clim, gauge, levels, rating }) {
  const flow = gauge?.flow;
  if (!flow) return null;
  const ft = gauge.stage?.v ?? rating?.stage(flow.v) ?? null;
  const cat = categoryAt(levels, ft);
  const cls = flowClass(clim, flow.t, flow.v);
  const dayAgo = gauge.series.find((p) => p.t >= new Date(flow.t.getTime() - 24 * HOUR));
  const dPct = dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;
  return {
    flow,
    ft,
    cat,
    dPct,
    normal: normalFor(clim, flow.t),
    label: cat ? (cat.key === 'action' ? 'Action stage' : cat.label) : (cls?.label ?? '—'),
    color: cat ? cat.color : cls ? CLASS_COLOR[cls.tone]() : C.faint,
  };
}

/** The forecast's 7-day peak (median) with its flood category, in the same fixed shape. */
export function outlook({ f, levels, rating }) {
  const ahead = f.fan.filter((r) => r.t > f.issue);
  const peak = ahead.reduce((m, r) => (r.q50 > m.q50 ? r : m), ahead[0]);
  const start = f.fan[0]?.q50 ?? ahead[0].q50;
  const cat = rating ? categoryAt(levels, rating.stage(peak.q50)) : null;
  const peaksNow = peak.t - f.issue <= 6 * HOUR || peak.q50 <= start * 1.03;
  const end = f.fan.at(-1);
  if (peaksNow) {
    const action = levels[0]?.cfs;
    const drops = action && start >= action ? ahead.find((r) => r.q50 < action) : null;
    return {
      peaksNow,
      value: end.q50,
      caption: 'In 7 days',
      detail: drops ? `Below action stage ${partOfDay(drops.t)}` : `Likely ${fmt.cfs(end.q25)}–${fmt.cfs(end.q75)} cfs`,
      label: end.q50 < start * 0.9 ? 'Falling' : 'Steady',
      color: C.melt,
    };
  }
  return {
    peaksNow,
    value: peak.q50,
    caption: 'Peak',
    detail: `${partOfDay(peak.t)} · likely ${fmt.cfs(peak.q25)}–${fmt.cfs(peak.q75)} cfs`,
    label: cat ? (cat.key === 'action' ? 'Rising to action stage' : `Rising to ${cat.label.toLowerCase()}`) : 'Rising, below flood stage',
    color: cat ? cat.color : C.rain,
  };
}
