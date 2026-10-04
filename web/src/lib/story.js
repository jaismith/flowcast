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

const MIN_WATER_IN = 0.1;
// Water temperature mostly follows the air; rain only gets the credit for a cooling week when there is a lot of it.
const MIN_COOLING_RAIN_IN = 0.5;

/**
 * The basin weather layer behind the forecast, by a stated rule rather than model attributions (the model has
 * none). Flow: the most water (inches) reaching the basin while the forecast changes, counting only what falls
 * before the peak when it rises: forecast rain, forecast snowmelt, or rain in the past day; failing that, snow
 * on the way (the precipitation layer), the snowpack, or the rain ahead. Water temperature: heat when the week's
 * daily highs warm, and when they cool, whichever of cold snowmelt or heavy rain is larger if there is enough,
 * otherwise cooler air.
 * Returns { key, why }.
 */
export function basinDriver({ view, f, tf, next }) {
  const ahead = (k, until = Infinity) => f.water.filter((w) => w.t0 < until).reduce((s, w) => s + w[k], 0);
  if (view === 'temp') {
    const highs = tf.highs;
    const delta = highs.length > 1 ? highs.at(-1).q50 - highs[0].q50 : 0;
    if (delta >= 2) return { key: 'airTemp', why: `Warmer air is heating the river: daily highs climb ${Math.round(delta)}°F this week.` };
    if (delta > -2) return { key: 'airTemp', why: 'Water temperature is following the air, with little change this week.' };
    const melt = ahead('melt');
    const rain = ahead('rain');
    const rainy = rain >= MIN_COOLING_RAIN_IN;
    if (melt >= MIN_WATER_IN && !(rainy && rain > melt)) return { key: 'snowDepth', why: `${fmt.in(melt)} of cold snowmelt is cooling the river.` };
    if (rainy) return { key: 'rainNext', why: `${fmt.in(rain)} of rain is cooling the river.` };
    return { key: 'airTemp', why: `Cooler air is cooling the river: daily highs drop ${Math.round(-delta)}°F this week.` };
  }

  const rising = !next.peaksNow;
  const until = rising ? f.peak.t.getTime() : Infinity;
  const dayAgo = f.issue.getTime() - 24 * HOUR;
  const inputs = [
    { key: 'rainNext', v: ahead('rain', until), what: 'of rain in the forecast' },
    { key: 'snowDepth', v: ahead('melt', until), what: 'of snowmelt' },
    { key: 'rain24', v: f.pastRain.filter((w) => w.t0 >= dayAgo).reduce((s, w) => s + w.rain, 0), what: 'of rain in the past day' },
  ];
  const top = inputs.reduce((m, c) => (c.v > m.v ? c : m));
  if (top.v >= MIN_WATER_IN) {
    const role = rising ? `is the biggest driver of the rise to the ${partOfDay(f.peak.t)} peak` : 'is the most water reaching the basin this week';
    return { key: top.key, why: `${fmt.in(top.v)} ${top.what} ${role}.` };
  }
  const snow = ahead('snow');
  if (snow >= MIN_WATER_IN) return { key: 'rainNext', why: `${fmt.in(snow)} of water is coming as snow, building the snowpack rather than the river.` };
  if (f.sweIn >= 0.5) return { key: 'snowDepth', why: `Little rain or melt this week; the snowpack holds ${fmt.in(f.sweIn)} of water.` };
  return { key: 'rainNext', why: 'Little rain or snowmelt is reaching the basin this week.' };
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
