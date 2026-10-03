import { categoryAt } from './rating.js';
import { flowClass, fmt, normalFor } from './data.js';

const HOUR = 3600 * 1000;

function partOfMonth(d) {
  const day = Number(d.toLocaleDateString('en-US', { day: 'numeric', timeZone: 'America/New_York' }));
  const month = d.toLocaleDateString('en-US', { month: 'long', timeZone: 'America/New_York' });
  return `${day <= 10 ? 'early' : day <= 20 ? 'mid-' : 'late'}${day <= 10 || day > 20 ? ' ' : ''}${month}`;
}

const weekdayTime = (d) => {
  const h = Number(d.toLocaleString('en-US', { hour: 'numeric', hour12: false, timeZone: 'America/New_York' }));
  const wd = d.toLocaleDateString('en-US', { weekday: 'long', timeZone: 'America/New_York' });
  return `${wd} ${h < 5 ? 'night' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening'}`;
};

/**
 * The page's lede: a headline and a list of dek fragments ({ text } or { strong }) describing the river now and,
 * when `f` is the forecast issued now, where it is headed.
 */
export function buildStory({ meta, clim, gauge, levels, rating, f }) {
  const flow = gauge?.flow;
  if (!flow) return { headline: `${meta.river} at ${meta.place}`, dek: [] };
  const river = meta.river.replace(/ River$/, '');
  const ft = gauge.stage?.v ?? rating?.stage(flow.v) ?? null;
  const cat = categoryAt(levels, ft);
  const when = partOfMonth(flow.t);
  const normal = normalFor(clim, flow.t);
  const cls = flowClass(clim, flow.t, flow.v);
  const dayAgo = gauge.series.find((p) => p.t >= new Date(flow.t.getTime() - 24 * HOUR));
  const dFt = rating && dayAgo ? rating.stage(flow.v) - rating.stage(dayAgo.v) : null;
  const dPct = dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;

  const crest = f && f.peak.t - f.issue > 6 * HOUR && f.peak.q50 > flow.v * 1.05 ? f.peak : null;
  const crestFt = crest && rating?.stage(crest.q50);
  const crestCat = categoryAt(levels, crestFt);

  let headline;
  if (cat && cat.key !== 'action') headline = `The ${river} is in ${cat.label.toLowerCase()} at ${meta.short}`;
  else if (cat) headline = `The ${river} is running high at ${meta.short}, above action stage`;
  else if (crestCat) headline = `The ${river} is rising toward ${crestCat.key === 'action' ? 'action stage' : crestCat.label.toLowerCase()}`;
  else if ((dFt ?? 0) > 1 || (dPct ?? 0) > 0.5) headline = `The ${river} is rising at ${meta.short}`;
  else if (cls?.tone === 'high') headline = `The ${river} is running high for ${when}`;
  else if (cls?.tone === 'low') headline = `The ${river} is running low for ${when}`;
  else headline = `The ${river} is running about normal for ${when}`;

  const dek = [{ text: 'It’s carrying ' }, { strong: `${fmt.cfs(flow.v)} cubic feet per second` }];
  const ratio = flow.v / normal.p50;
  if (ratio >= 1.5) dek.push({ text: ', about ' }, { strong: `${ratio >= 3 ? Math.round(ratio) : ratio.toFixed(1)} times` }, { text: ' the normal for this date' });
  else if (ratio <= 0.67) dek.push({ text: ', about ' }, { strong: `${Math.round(ratio * 100)}%` }, { text: ' of normal for this date' });
  else dek.push({ text: `, close to normal for this date (typically ${fmt.cfs(normal.p25)}–${fmt.cfs(normal.p75)})` });
  if (dPct != null && Math.abs(dPct) >= 0.05) {
    const big = Math.abs(dPct) >= 1;
    dek.push({ text: `, ${dPct > 0 ? 'up' : 'down'} ` }, { strong: big ? `${(flow.v / dayAgo.v).toFixed(1)}-fold` : `${Math.round(Math.abs(dPct) * 100)}%` }, { text: ' since yesterday' });
  }
  dek.push({ text: '.' });
  if (ft != null && levels.length) {
    dek.push({ text: ' On the gauge that’s ' }, { strong: `${ft.toFixed(1)} feet` });
    dek.push({ text: cat ? `, ${cat.key === 'action' ? 'above action stage' : `in ${cat.label.toLowerCase()}`}.` : `, ${(levels[0].ft - ft).toFixed(1)} feet below action stage.` });
  }

  if (f) {
    if (crest) {
      dek.push({ text: ' The forecast has it cresting near ' }, { strong: `${fmt.cfs(crest.q50)} cfs` }, { text: ` ${weekdayTime(crest.t)}` });
      dek.push({ text: crestCat ? `, ${crestCat.key === 'action' ? 'above action stage' : `in ${crestCat.label.toLowerCase()}`}.` : ', below flood stage.' });
    } else {
      const end = f.fan.at(-1);
      dek.push({ text: ' The forecast has it ' }, { strong: `${end.q50 < flow.v * 0.9 ? 'falling' : 'holding steady'}` });
      dek.push({ text: `, to about ${fmt.cfs(end.q50)} cfs by ${weekdayTime(end.t).split(' ')[0]}` });
      const minor = levels.find((l) => l.key === 'minor');
      const below = cat && minor && f.fan.find((r) => r.t > f.issue && rating.stage(r.q50) < minor.ft);
      dek.push({ text: below && ft >= minor.ft ? `, back below flood stage by ${weekdayTime(below.t)}.` : '.' });
    }
  }
  return { headline, dek, ft, dFt, dPct, cat };
}
