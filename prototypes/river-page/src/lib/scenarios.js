import { normalFor, replayIssues } from './data.js';

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;

/** Simulated "now" must leave a week of history behind it and a full 7-day forecast window ahead. */
export function simRange(data) {
  const { hindcast: hc, observed: obs } = data;
  const list = replayIssues(hc);
  return { min: new Date(obs.flow.t0 * 1000 + 7 * DAY), max: new Date(hc.issues[list.at(-1)] * 1000) };
}

/** The latest regular forecast issued at or before `at`. */
export function issueAtOrBefore(hc, at) {
  const list = replayIssues(hc);
  let best = list[0];
  for (const i of list) if (hc.issues[i] * 1000 <= at.getTime()) best = i;
  return best;
}

function dailyMeans(series) {
  const out = [];
  for (let k = 0; k + 24 <= series.v.length; k += 24) {
    const day = series.v.slice(k, k + 24).filter((v) => v != null);
    if (day.length >= 18) out.push({ t: new Date(series.t0 * 1000 + k * HOUR), v: day.reduce((s, v) => s + v, 0) / day.length });
  }
  return out;
}

const at12 = (ms) => {
  const d = new Date(ms);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 12));
};
const month = (d) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const k = (v) => `${Math.round(v / 1000)}k cfs`;

/** Interesting moments in the validation years, found from the site's own data. */
export function findScenarios(data) {
  const { hindcast: hc, observed: obs, clim } = data;
  const range = simRange(data);
  const clamp = (d) => new Date(Math.min(range.max, Math.max(range.min, d)));
  const out = [];
  const add = (s) => out.push({ ...s, at: clamp(s.at) });

  const floods = hc.events.filter((e) => e.kind === 'flood').sort((a, b) => b.value - a.value);
  floods.slice(0, 2).forEach((e, i) => {
    const peak = new Date(e.time * 1000);
    add({
      key: `flood-${i}`,
      group: 'Floods',
      label: i ? 'Second-biggest flood' : 'Biggest flood',
      detail: `${month(peak)} · ${k(e.value)} peak, 2 days out`,
      at: at12(e.time * 1000 - 2 * DAY),
    });
  });
  if (floods[0]) {
    add({
      key: 'flood-peak',
      group: 'Floods',
      label: 'At the flood crest',
      detail: `${month(new Date(floods[0].time * 1000))} · ${k(floods[0].value)}`,
      at: new Date(floods[0].time * 1000),
    });
  }

  const pos = replayIssues(hc);
  const nBins = 168 / hc.precip.bin_h;
  const nF = hc.precip.fields.length;
  let wettest = null;
  let snowiest = null;
  for (const i of pos) {
    let rain = 0;
    let snow = 0;
    for (let b = 0; b < nBins; b++) {
      const base = (i * nBins + b) * nF;
      const m = hc.precip.bins[base] ?? 0;
      const s = hc.precip.bins[base + 2] ?? 0;
      rain += m * (1 - s);
      snow += m * s;
    }
    if (!wettest || rain > wettest.v) wettest = { i, v: rain };
    if (!snowiest || snow > snowiest.v) snowiest = { i, v: snow };
  }
  if (wettest) {
    add({
      key: 'rain-forecast',
      group: 'Rain & snow',
      label: 'Wettest forecast',
      detail: `${month(new Date(hc.issues[wettest.i] * 1000))} · ${(wettest.v / 25.4).toFixed(1)} in rain in 7 days`,
      at: new Date(hc.issues[wettest.i] * 1000),
    });
  }
  if (snowiest && snowiest.v > 5) {
    add({
      key: 'snow-forecast',
      group: 'Rain & snow',
      label: 'Big snowstorm coming',
      detail: `${month(new Date(hc.issues[snowiest.i] * 1000))} · ${(snowiest.v / 25.4).toFixed(1)} in water as snow`,
      at: new Date(hc.issues[snowiest.i] * 1000),
    });
  }

  const swe = obs.swe.v.map((v, d) => ({ t: obs.swe.t0 * 1000 + d * obs.swe.step_h * HOUR, v }));
  const deepest = swe.reduce((m, p) => (p.v != null && p.v > (m?.v ?? -1) ? p : m), null);
  if (deepest && deepest.v > 20) {
    add({
      key: 'snowpack',
      group: 'Rain & snow',
      label: 'Deepest snowpack',
      detail: `${month(new Date(deepest.t))} · ${(deepest.v / 25.4).toFixed(1)} in water in the snow`,
      at: at12(deepest.t),
    });
    let melt = null;
    for (let d = 0; d + 3 < swe.length; d++) {
      const drop = (swe[d].v ?? 0) - (swe[d + 3].v ?? 0);
      if (!melt || drop > melt.v) melt = { t: swe[d].t, v: drop };
    }
    if (melt && melt.v > 10) {
      add({
        key: 'melt',
        group: 'Rain & snow',
        label: 'Fastest snowmelt',
        detail: `${month(new Date(melt.t))} · ${(melt.v / 25.4).toFixed(1)} in melts in 3 days`,
        at: at12(melt.t),
      });
    }
  }

  const days = dailyMeans(obs.flow).map((d) => ({ ...d, ratio: d.v / (normalFor(clim, d.t).p50 ?? d.v) }));
  // Summer and fall only: winter "low" days are often ice on the gauge, and the normal is large then.
  const warm = days.filter((d) => d.t.getUTCMonth() >= 5 && d.t.getUTCMonth() <= 10);
  const driest = warm.reduce((m, d) => (d.ratio < (m?.ratio ?? Infinity) ? d : m), null);
  if (driest) {
    add({
      key: 'drought',
      group: 'Low & warm',
      label: 'Drought low flow',
      detail: `${month(driest.t)} · ${Math.round(driest.ratio * 100)}% of normal`,
      at: at12(driest.t.getTime()),
    });
  }
  const heat = hc.events.find((e) => e.kind === 'heat');
  if (heat) {
    add({
      key: 'heat',
      group: 'Low & warm',
      label: 'Warmest water',
      detail: `${month(new Date(heat.time * 1000))} · ${Math.round((heat.value * 9) / 5 + 32)}°F`,
      at: at12(heat.time * 1000 - DAY),
    });
  }
  const typical = days.filter((d) => Math.abs(d.ratio - 1) < 0.05 && d.t.getUTCMonth() === 5);
  if (typical.length) {
    add({ key: 'typical', group: 'Low & warm', label: 'An ordinary June day', detail: `${month(typical[0].t)} · right at normal`, at: at12(typical[0].t.getTime()) });
  }
  return out;
}
