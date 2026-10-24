import * as d3 from 'd3';
import { fmt, fmtFlow, fmtPct, fmtTemp, toTemp, tooltip, units } from './common.js';

const H = 3600 * 1000;
const tip = () => tooltip();

function frame(el, height, margin) {
  el.innerHTML = '';
  const width = Math.max(280, el.clientWidth);
  const svg = d3.select(el).append('svg').attr('viewBox', `0 0 ${width} ${height}`).attr('height', height);
  const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);
  return { svg, g, w: width - margin.left - margin.right, h: height - margin.top - margin.bottom, width };
}

function xAxis(g, x, h, w) {
  const spanDays = (x.domain()[1] - x.domain()[0]) / 86400e3;
  const long = w >= 560 && spanDays <= 20;
  const step = Math.max(1, Math.ceil(spanDays / Math.max(2, Math.floor(w / (long ? 92 : 58)))));
  const ticks = x.ticks(d3.utcDay.every(step)).map((t) => new Date(+t + 12 * 3600e3)).filter((t) => t <= x.domain()[1]);
  const f = long ? (t) => fmt.day(t).replace(',', '') : (t) => d3.utcFormat('%b %-d')(t);
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).tickValues(ticks).tickFormat(f).tickSizeOuter(0));
}

function yAxis(g, y, w, format, label) {
  g.append('g').attr('class', 'grid').call(d3.axisLeft(y).ticks(5).tickSize(-w).tickFormat(''));
  const a = g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(5).tickFormat(format).tickSizeOuter(0));
  a.select('.domain').remove();
  if (label) g.append('text').attr('class', 'annot').attr('x', 0).attr('y', -8).text(label);
}

const flowTick = (v) => (v >= 1000 ? `${d3.format('~s')(v).replace('k', 'k')}` : d3.format('~r')(v));

// ---------------------------------------------------------------------------------------------- recent vs normal

export function recentChart(el, { t, v, bands, isTemp, now }) {
  const height = el.clientWidth < 520 ? 240 : 300;
  const m = { top: 22, right: 16, bottom: 26, left: 46 };
  const { g, w, h } = frame(el, height, m);
  const conv = isTemp ? toTemp : (x) => x;
  const pts = t.map((ti, i) => ({ t: new Date(ti * 1000), v: conv(v[i]) })).filter((p) => p.v != null);
  if (!pts.length) {
    el.innerHTML = '<p class="skeleton">No recent data from this gauge.</p>';
    return;
  }
  const t1 = now ?? pts[pts.length - 1].t;
  const t0 = new Date(+t1 - 30 * 86400e3);
  const days = d3.utcDays(new Date(+t0 - 86400e3), new Date(+t1 + 86400e3)).map((d) => ({ t: new Date(+d + 12 * H), b: bands(d) })).filter((d) => d.b);
  const shown = pts.filter((p) => p.t >= t0);
  const x = d3.scaleUtc().domain([t0, t1]).range([0, w]);
  const ymax = d3.max([...shown.map((p) => p.v), ...days.map((d) => conv(d.b[4]))]) * 1.08;
  const ymin = isTemp ? d3.min([...shown.map((p) => p.v), ...days.map((d) => conv(d.b[0]))]) - 1 : 0;
  const y = d3.scaleLinear().domain([ymin, ymax]).nice().range([h, 0]);
  yAxis(g, y, w, isTemp ? (d) => `${d}°` : flowTick, isTemp ? `°${units.temp}` : 'ft³/s');
  xAxis(g, x, h, w);
  const color = isTemp ? 'var(--temp)' : 'var(--blue)';
  const area = (lo, hi) => d3.area().x((d) => x(d.t)).y0((d) => y(conv(d.b[lo]))).y1((d) => y(conv(d.b[hi]))).curve(d3.curveMonotoneX);
  const clip = g.append('clipPath').attr('id', `clip-${el.id}`).append('rect').attr('width', w).attr('height', h);
  const plot = g.append('g').attr('clip-path', `url(#clip-${el.id})`);
  plot.append('path').datum(days).attr('d', area(0, 4)).attr('fill', '#e6edf3');
  plot.append('path').datum(days).attr('d', area(1, 3)).attr('fill', '#d3dfe9');
  plot.append('path').datum(days).attr('d', d3.line().x((d) => x(d.t)).y((d) => y(conv(d.b[2]))).curve(d3.curveMonotoneX)).attr('fill', 'none').attr('stroke', '#9fb2c3').attr('stroke-dasharray', '4 3');
  plot.append('path').datum(shown).attr('d', d3.line().defined((d) => d.v != null).x((d) => x(d.t)).y((d) => y(d.v))).attr('fill', 'none').attr('stroke', color).attr('stroke-width', 2);
  const key = g.append('g').attr('transform', `translate(${w - 230},-14)`).attr('class', 'annot');
  key.append('rect').attr('width', 12).attr('height', 8).attr('fill', '#d3dfe9');
  key.append('text').attr('x', 16).attr('y', 8).text('normal (middle half)');
  key.append('rect').attr('x', 130).attr('width', 12).attr('height', 8).attr('fill', '#e6edf3');
  key.append('text').attr('x', 146).attr('y', 8).text('80% of years');

  const tt = tip();
  const bis = d3.bisector((d) => d.t).center;
  const hoverLine = g.append('line').attr('class', 'now-line').attr('y1', 0).attr('y2', h).style('opacity', 0);
  const dot = g.append('circle').attr('r', 4).attr('fill', color).style('opacity', 0);
  g.append('rect').attr('width', w).attr('height', h).attr('fill', 'transparent')
    .on('pointermove', (e) => {
      const [mx] = d3.pointer(e);
      const p = shown[bis(shown, x.invert(mx))];
      const day = days[bis(days, p.t)];
      hoverLine.attr('x1', x(p.t)).attr('x2', x(p.t)).style('opacity', 1);
      dot.attr('cx', x(p.t)).attr('cy', y(p.v)).style('opacity', 1);
      const val = isTemp ? `${p.v.toFixed(1)}°${units.temp}` : fmtFlow(p.v);
      const norm = day ? (isTemp ? `${conv(day.b[1]).toFixed(1)}–${conv(day.b[3]).toFixed(1)}°` : `${fmtFlow(day.b[1], false)}–${fmtFlow(day.b[3])}`) : '–';
      tt.show(`<b>${val}</b><br><span class="muted">${fmt.dayTime(p.t)}</span><br>Normal range ${norm}`, e.clientX, e.clientY);
    })
    .on('pointerleave', () => { tt.hide(); hoverLine.style('opacity', 0); dot.style('opacity', 0); });
}

// ---------------------------------------------------------------------------------------------- fan chart

/**
 * q: [lead][5] quantiles (5, 25, 50, 75, 95) at `leads` hours after `issue`.
 * obs: {t0 (s), v[]} hourly. nws: optional {t[], v[]}. daily: optional [{t0, t1, q[5], obs}].
 */
export function fanChart(el, { issue, leads, q, obs, nws, isTemp, daily, pastH = 72, onHover }) {
  const narrow = el.clientWidth < 520;
  const height = narrow ? 250 : 300;
  const m = { top: 22, right: 14, bottom: 26, left: 48 };
  const { g, w, h } = frame(el, height, m);
  const conv = isTemp ? toTemp : (x) => x;
  const t0 = new Date(+issue - pastH * H);
  const t1 = new Date(+issue + 168 * H);
  const x = d3.scaleUtc().domain([t0, t1]).range([0, w]);
  const series = (from, to) => {
    const out = [];
    const i0 = Math.max(0, Math.floor((from / 1000 - obs.t0) / 3600));
    const i1 = Math.min(obs.v.length - 1, Math.ceil((to / 1000 - obs.t0) / 3600));
    for (let i = i0; i <= i1; i++) out.push({ t: new Date((obs.t0 + i * 3600) * 1000), v: conv(obs.v[i]) });
    return out;
  };
  const before = series(+t0, +issue);
  const after = series(+issue, +t1);
  const fc = [{ t: issue, q: null }, ...leads.map((l, i) => ({ t: new Date(+issue + l * H), q: q[i].map(conv) }))].filter((d, i) => i === 0 || d.q.every((v) => v != null));
  const last = before.filter((d) => d.v != null).at(-1);
  fc[0].q = last ? [last.v, last.v, last.v, last.v, last.v] : fc[1]?.q;
  const nwsPts = nws ? nws.t.map((ti, i) => ({ t: new Date(ti * 1000), v: nws.v[i] })).filter((p) => p.t >= issue && p.t <= t1) : [];
  if (nwsPts.length && last) nwsPts.unshift({ t: last.t, v: last.v });
  const vals = [...before, ...after].map((d) => d.v).concat(fc.flatMap((d) => (d.q ? [d.q[0], d.q[4]] : [])), nwsPts.map((p) => p.v), (daily || []).flatMap((d) => [conv(d.q[4]), conv(d.obs)]));
  const finite = vals.filter((v) => v != null && Number.isFinite(v));
  const lo = isTemp ? d3.min(finite) - 1 : 0;
  const hi = d3.max(finite) * (isTemp ? 1 : 1.06) + (isTemp ? 1 : 0);
  const y = d3.scaleLinear().domain([lo, hi]).nice().range([h, 0]);
  yAxis(g, y, w, isTemp ? (d) => `${d}°` : flowTick, isTemp ? `°${units.temp}` : 'ft³/s');
  xAxis(g, x, h, w);

  const plot = g.append('g');
  plot.append('rect').attr('x', x(issue)).attr('width', w - x(issue)).attr('height', h).attr('fill', '#f7fafd');
  const c90 = isTemp ? 'var(--temp-90)' : 'var(--band-90)';
  const c50 = isTemp ? 'var(--temp-50)' : 'var(--band-50)';
  const cMed = isTemp ? 'var(--temp)' : 'var(--blue)';
  const band = (a, b) => d3.area().x((d) => x(d.t)).y0((d) => y(d.q[a])).y1((d) => y(d.q[b])).curve(d3.curveMonotoneX);
  plot.append('path').datum(fc).attr('d', band(0, 4)).attr('fill', c90);
  plot.append('path').datum(fc).attr('d', band(1, 3)).attr('fill', c50);
  if (daily) {
    for (const dd of daily) {
      if (dd.q.some((v) => v == null)) continue;
      const xa = x(dd.t0);
      const xb = x(dd.t1);
      if (xb < x(issue) || xa > w) continue;
      plot.append('rect').attr('x', Math.max(xa, x(issue))).attr('width', Math.max(0, Math.min(xb, w) - Math.max(xa, x(issue)))).attr('y', y(conv(dd.q[3]))).attr('height', Math.max(1.5, y(conv(dd.q[1])) - y(conv(dd.q[3])))).attr('fill', 'none').attr('stroke', cMed).attr('stroke-width', 1.2).attr('rx', 2);
      plot.append('line').attr('x1', (xa + xb) / 2).attr('x2', (xa + xb) / 2).attr('y1', y(conv(dd.q[4]))).attr('y2', y(conv(dd.q[0]))).attr('stroke', cMed).attr('stroke-opacity', 0.45);
      if (dd.obs != null) plot.append('path').attr('d', d3.symbol(d3.symbolDiamond, 34)()).attr('transform', `translate(${(xa + xb) / 2},${y(conv(dd.obs))})`).attr('fill', 'var(--obs)');
    }
  }
  plot.append('path').datum(fc).attr('d', d3.line().x((d) => x(d.t)).y((d) => y(d.q[2])).curve(d3.curveMonotoneX)).attr('fill', 'none').attr('stroke', cMed).attr('stroke-width', 2.2);
  if (nwsPts.length) plot.append('path').datum(nwsPts).attr('d', d3.line().x((d) => x(d.t)).y((d) => y(d.v))).attr('fill', 'none').attr('stroke', 'var(--nws)').attr('stroke-width', 2).attr('stroke-dasharray', '6 4');
  const line = d3.line().defined((d) => d.v != null).x((d) => x(d.t)).y((d) => y(d.v));
  plot.append('path').datum(before).attr('d', line).attr('fill', 'none').attr('stroke', 'var(--obs)').attr('stroke-width', 1.8);
  plot.append('path').datum(after).attr('d', line).attr('fill', 'none').attr('stroke', 'var(--obs)').attr('stroke-width', 1.6).attr('stroke-dasharray', '1.5 3').attr('stroke-linecap', 'round');
  g.append('line').attr('class', 'now-line').attr('x1', x(issue)).attr('x2', x(issue)).attr('y1', -6).attr('y2', h);
  g.append('text').attr('class', 'annot').attr('x', x(issue) + 4).attr('y', -8).text('forecast issued');

  const tt = tip();
  const cross = g.append('line').attr('class', 'now-line').attr('y1', 0).attr('y2', h).style('opacity', 0);
  const bis = d3.bisector((d) => d.t).center;
  const all = [...before, ...after];
  const fcFmt = (v) => (isTemp ? `${v.toFixed(1)}°` : fmtFlow(v, false));
  g.append('rect').attr('width', w).attr('height', h).attr('fill', 'transparent')
    .on('pointermove', (e) => {
      const [mx] = d3.pointer(e);
      const t = x.invert(mx);
      cross.attr('x1', mx).attr('x2', mx).style('opacity', 1);
      const o = all[bis(all, t)];
      let html = `<span class="muted">${fmt.dayTime(t)}</span>`;
      if (t > issue) {
        const k = Math.min(fc.length - 1, Math.max(1, bis(fc, t)));
        const d = fc[k];
        const lead = Math.round((d.t - issue) / H);
        html += `<br>Forecast (${lead < 48 ? `${lead} h` : `${(lead / 24).toFixed(lead % 24 ? 1 : 0)} days`} ahead): <b>${fcFmt(d.q[2])}</b><br><span class="muted">likely ${fcFmt(d.q[1])}–${fcFmt(d.q[3])}, 90% ${fcFmt(d.q[0])}–${fcFmt(d.q[4])}</span>`;
        if (nwsPts.length > 1) {
          const n = nwsPts[d3.bisector((p) => p.t).center(nwsPts, t)];
          if (Math.abs(n.t - t) < 4 * H) html += `<br>NWS: <b>${fcFmt(n.v)}</b>`;
        }
      }
      if (o && o.v != null && Math.abs(o.t - t) < 2 * H) html += `<br>Observed: <b>${isTemp ? `${o.v.toFixed(1)}°${units.temp}` : fmtFlow(o.v)}</b>`;
      tt.show(html, e.clientX, e.clientY);
      onHover?.(t);
    })
    .on('pointerleave', () => { tt.hide(); cross.style('opacity', 0); onHover?.(null); });
  return { x, margin: m };
}

// ---------------------------------------------------------------------------------------------- precipitation strip

export function precipStrip(el, { issue, binH, bins, pastH = 72, totals }) {
  const height = 76;
  const m = { top: 6, right: 14, bottom: 14, left: 48 };
  const { g, w, h } = frame(el, height, m);
  const t0 = new Date(+issue - pastH * H);
  const t1 = new Date(+issue + 168 * H);
  const x = d3.scaleUtc().domain([t0, t1]).range([0, w]);
  const data = bins.map((b, i) => ({ t: new Date(+issue + i * binH * H), mean: b[0], p90: b[1], snow: b[2] })).filter((d) => d.mean != null);
  const inch = units.temp === 'F';
  const conv = (mm) => (inch ? mm / 25.4 : mm);
  const ymax = Math.max(inch ? 0.25 : 6, d3.max(data, (d) => conv(d.p90)) || 0);
  const y = d3.scaleLinear().domain([0, ymax]).nice().range([h, 0]);
  const a = g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(2).tickSizeOuter(0).tickFormat((v) => (inch ? d3.format('.2~f')(v) : d3.format('~r')(v))));
  a.select('.domain').remove();
  g.append('line').attr('x1', 0).attr('x2', w).attr('y1', h).attr('y2', h).attr('stroke', 'var(--line)');
  const bw = Math.max(2, x(new Date(+t0 + binH * H)) - x(t0) - 2);
  const bars = g.append('g');
  for (const d of data) {
    const xx = x(d.t) + 1;
    const total = conv(d.mean);
    const snowH = total * (d.snow || 0);
    bars.append('line').attr('x1', xx + bw / 2).attr('x2', xx + bw / 2).attr('y1', y(conv(d.p90))).attr('y2', y(total)).attr('stroke', '#9fb2c3');
    bars.append('rect').attr('x', xx).attr('width', bw).attr('y', y(total)).attr('height', h - y(total)).attr('fill', '#4d8fe0');
    if (snowH > 0) bars.append('rect').attr('x', xx).attr('width', bw).attr('y', y(total)).attr('height', h - y(snowH)).attr('fill', 'var(--snow)');
  }
  const head = d3.select(el).insert('div', 'svg').attr('class', 'strip-head');
  head.append('span').html(`Forecast rain <i class="sw" style="background:#4d8fe0"></i> and snow <i class="sw" style="background:var(--snow)"></i> over the basin, ${inch ? 'in' : 'mm'} per ${binH} h (GEFS mean; whisker = wettest 10%)`);
  if (totals && totals[0] != null) {
    const f = (mm) => (inch ? (mm / 25.4).toFixed(2) : mm.toFixed(0));
    head.append('span').html(`7-day total <b>${f(totals[0])} ${inch ? 'in' : 'mm'}</b> (10–90%: ${f(totals[1])}–${f(totals[2])})`);
  }
  g.append('line').attr('class', 'now-line').attr('x1', x(issue)).attr('x2', x(issue)).attr('y1', 0).attr('y2', h);
  if (!data.length) g.append('text').attr('class', 'annot').attr('x', x(issue) + 6).attr('y', h / 2).text('No GEFS run available for this issue');
}

// ---------------------------------------------------------------------------------------------- peak evolution

/** points: [{ issue: Date, hBefore, q[5] }] forecasts valid at the observed peak time; `current` marks the selected issue. */
export function evolutionChart(el, { points, peak, peakTime, current, onPick }) {
  const height = el.clientWidth < 520 ? 210 : 230;
  const m = { top: 26, right: 16, bottom: 30, left: 48 };
  const { g, w, h } = frame(el, height, m);
  if (!points.length) return;
  const x = d3.scaleLinear().domain([d3.max(points, (p) => p.hBefore) + 6, 0]).range([0, w]);
  const y = d3.scaleLinear().domain([0, Math.max(peak, d3.max(points, (p) => p.q[4])) * 1.08]).nice().range([h, 0]);
  yAxis(g, y, w, flowTick, 'ft³/s at the time of the peak');
  const ticks = [168, 144, 120, 96, 72, 48, 24, 12, 0].filter((v) => v <= x.domain()[0] && (w > 420 || v % 48 === 0 || v === 0));
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).tickValues(ticks).tickFormat((v) => (v === 0 ? 'peak' : v % 24 === 0 ? `${v / 24} d` : `${v} h`)).tickSizeOuter(0));
  g.append('text').attr('class', 'annot').attr('x', w).attr('y', h + 28).attr('text-anchor', 'end').text('forecast issued this long before the peak →');
  g.append('line').attr('x1', 0).attr('x2', w).attr('y1', y(peak)).attr('y2', y(peak)).attr('stroke', 'var(--obs)').attr('stroke-dasharray', '1.5 3').attr('stroke-width', 1.6);
  g.append('text').attr('class', 'annot').attr('x', 4).attr('y', y(peak) - 5).style('fill', 'var(--ink)').text(`observed peak ${fmtFlow(peak)}, ${fmt.dayTime(peakTime)}`);
  const bw = Math.max(3, Math.min(14, (w / points.length) * 0.5));
  const tt = tip();
  const gs = g.selectAll('g.ev').data(points).join('g').attr('class', 'ev').attr('transform', (d) => `translate(${x(d.hBefore)},0)`).style('cursor', 'pointer');
  gs.append('line').attr('y1', (d) => y(d.q[4])).attr('y2', (d) => y(d.q[0])).attr('stroke', 'var(--blue-2)').attr('stroke-width', 1.4);
  gs.append('rect').attr('x', -bw / 2).attr('width', bw).attr('y', (d) => y(d.q[3])).attr('height', (d) => Math.max(1.5, y(d.q[1]) - y(d.q[3]))).attr('fill', 'var(--band-50)').attr('rx', 2)
    .attr('stroke', (d) => (+d.issue === +current ? 'var(--navy)' : 'none')).attr('stroke-width', 2);
  gs.append('circle').attr('cy', (d) => y(d.q[2])).attr('r', (d) => (+d.issue === +current ? 4.5 : 3)).attr('fill', 'var(--blue)');
  gs.append('rect').attr('x', -bw).attr('width', bw * 2).attr('height', h).attr('fill', 'transparent')
    .on('pointerenter pointermove', (e, d) => tt.show(`<b>Issued ${fmt.dayTime(d.issue)}</b><br>${Math.round(d.hBefore)} h before the peak<br>Middle forecast ${fmtFlow(d.q[2])}<br><span class="muted">90% range ${fmtFlow(d.q[0], false)}–${fmtFlow(d.q[4])}</span><br><span class="muted">Click to open this forecast</span>`, e.clientX, e.clientY))
    .on('pointerleave', () => tt.hide())
    .on('click', (e, d) => onPick?.(d.issue));
}

// ---------------------------------------------------------------------------------------------- skill

const LEAD_TICKS = [1, 3, 6, 12, 24, 48, 72, 120, 168];
const leadLabel = (l) => (l < 24 ? `${l}h` : `${l / 24}d`);

export function skillChart(el, { flow, temp, national }) {
  const narrow = el.clientWidth < 520;
  const height = narrow ? 290 : 290;
  const m = { top: narrow ? 58 : 24, right: 16, bottom: 30, left: 44 };
  const { g, w, h } = frame(el, height, m);
  const x = d3.scaleLog().domain([1, 168]).range([0, w]);
  const all = [...flow.map((r) => r.skill_lo ?? r.skill), ...temp.map((r) => r.skill)];
  const y = d3.scaleLinear().domain([Math.min(0, d3.min(all)), 1]).nice().range([h, 0]);
  g.append('g').attr('class', 'grid').call(d3.axisLeft(y).ticks(5).tickSize(-w).tickFormat(''));
  g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(5).tickFormat(d3.format('.0%'))).select('.domain').remove();
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).tickValues(LEAD_TICKS).tickFormat(leadLabel).tickSizeOuter(0));
  g.append('text').attr('class', 'annot').attr('x', w).attr('y', h + 28).attr('text-anchor', 'end').text('forecast lead time →');
  g.append('line').attr('x1', 0).attr('x2', w).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', '#9fb2c3');
  const nat = Object.entries(national).map(([k, v]) => ({ l: +k, s: v }));
  g.append('path').datum(nat).attr('d', d3.line().x((d) => x(d.l)).y((d) => y(d.s))).attr('fill', 'none').attr('stroke', '#9fb2c3').attr('stroke-width', 1.6).attr('stroke-dasharray', '5 4');
  g.append('path').datum(flow).attr('d', d3.area().x((d) => x(d.lead_h)).y0((d) => y(d.skill_lo)).y1((d) => y(d.skill_hi))).attr('fill', 'var(--band-90)');
  g.append('path').datum(flow).attr('d', d3.line().x((d) => x(d.lead_h)).y((d) => y(d.skill))).attr('fill', 'none').attr('stroke', 'var(--blue)').attr('stroke-width', 2.4);
  g.append('path').datum(temp).attr('d', d3.line().x((d) => x(d.lead_h)).y((d) => y(d.skill))).attr('fill', 'none').attr('stroke', 'var(--temp)').attr('stroke-width', 2);
  const key = g.append('g').attr('transform', 'translate(0,-12)').attr('class', 'annot');
  [['var(--blue)', 'Flow, this river', ''], ['var(--temp)', 'Water temperature', ''], ['#9fb2c3', 'Flow, median of 552 rivers', '5 4']].forEach(([c, label, dash], i) => {
    const gx = key.append('g').attr('transform', narrow ? `translate(0,${(i - 2) * 15})` : `translate(${i * 150},0)`);
    gx.append('line').attr('x1', 0).attr('x2', 16).attr('y1', 0).attr('y2', 0).attr('stroke', c).attr('stroke-width', 2.2).attr('stroke-dasharray', dash);
    gx.append('text').attr('x', 20).attr('y', 4).text(label);
  });
  const tt = tip();
  const pts = flow.map((r) => ({ ...r, kind: 'flow' })).concat(temp.map((r) => ({ ...r, kind: 'temp' })));
  g.selectAll('circle.pt').data(pts).join('circle').attr('class', 'pt').attr('cx', (d) => x(d.lead_h)).attr('cy', (d) => y(d.skill)).attr('r', 3.2)
    .attr('fill', (d) => (d.kind === 'flow' ? 'var(--blue)' : 'var(--temp)')).attr('stroke', '#fff')
    .on('pointerenter pointermove', (e, d) => tt.show(d.kind === 'flow'
      ? `<b>Flow, ${leadLabel(d.lead_h)} ahead</b><br>Skill vs persistence ${fmtPct(d.skill)} <span class="muted">(95%: ${fmtPct(d.skill_lo)}–${fmtPct(d.skill_hi)})</span><br>Typical miss of the middle forecast: ${fmtFlow(d.mae_median)}<br>90% range caught the flow ${fmtPct(d.cover90)} of the time<br><span class="muted">${d.n.toLocaleString()} forecasts</span>`
      : `<b>Water temperature, ${leadLabel(d.lead_h)} ahead</b><br>Skill vs same-hour-yesterday ${fmtPct(d.skill)}<br>Typical miss ${(d.mae_median * (units.temp === 'F' ? 1.8 : 1)).toFixed(1)}°${units.temp}<br><span class="muted">${d.n.toLocaleString()} forecasts</span>`, e.clientX, e.clientY))
    .on('pointerleave', () => tt.hide());
}

export function nwsChart(el, rows) {
  const height = el.clientWidth < 520 ? 240 : 270;
  const m = { top: 16, right: 12, bottom: 30, left: 44 };
  const { g, w, h } = frame(el, height, m);
  const x = d3.scaleBand().domain(rows.map((r) => r.lead_h)).range([0, w]).padding(0.32);
  const y = d3.scaleLinear().domain([Math.min(-0.2, d3.min(rows, (r) => r.skill_lo)), Math.max(0.5, d3.max(rows, (r) => r.skill_hi))]).nice().range([h, 0]);
  g.append('g').attr('class', 'grid').call(d3.axisLeft(y).ticks(5).tickSize(-w).tickFormat(''));
  g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(5).tickFormat(d3.format('.0%'))).select('.domain').remove();
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).tickFormat(leadLabel).tickSizeOuter(0));
  g.append('line').attr('x1', 0).attr('x2', w).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', '#6b7a87');
  const tt = tip();
  const bars = g.selectAll('g.bar').data(rows).join('g').attr('class', 'bar').attr('transform', (d) => `translate(${x(d.lead_h)},0)`);
  bars.append('rect').attr('width', x.bandwidth()).attr('y', (d) => y(Math.max(0, d.skill))).attr('height', (d) => Math.abs(y(d.skill) - y(0))).attr('rx', 3)
    .attr('fill', (d) => (d.skill_lo > 0 ? 'var(--blue)' : '#9cc3f0'));
  bars.append('line').attr('x1', x.bandwidth() / 2).attr('x2', x.bandwidth() / 2).attr('y1', (d) => y(d.skill_lo)).attr('y2', (d) => y(d.skill_hi)).attr('stroke', '#0b2a3f').attr('stroke-width', 1.3);
  bars.append('text').attr('class', 'annot').attr('x', x.bandwidth() / 2).attr('y', (d) => y(Math.max(d.skill_hi, 0)) - 5).attr('text-anchor', 'middle').style('font-weight', 600).style('fill', 'var(--ink-2)').text((d) => `${d.skill >= 0 ? '+' : ''}${Math.round(d.skill * 100)}%`);
  bars.append('rect').attr('width', x.bandwidth()).attr('height', h).attr('fill', 'transparent')
    .on('pointerenter pointermove', (e, d) => tt.show(`<b>${leadLabel(d.lead_h)} ahead</b><br>flowcast vs NWS: ${d.skill >= 0 ? '+' : ''}${(d.skill * 100).toFixed(0)}% <span class="muted">(95%: ${(d.skill_lo * 100).toFixed(0)} to ${(d.skill_hi * 100).toFixed(0)}%)</span><br>flowcast error ${fmtFlow(d.crps_model)}, NWS ${fmtFlow(d.mae_marfc)}<br><span class="muted">${d.n} NWS forecasts</span>`, e.clientX, e.clientY))
    .on('pointerleave', () => tt.hide());
}

export function reliabilityChart(el, rows) {
  const height = el.clientWidth < 520 ? 240 : 270;
  const m = { top: 24, right: 12, bottom: 30, left: 44 };
  const { g, w, h } = frame(el, height, m);
  const x = d3.scaleLog().domain([1, 168]).range([0, w]);
  const y = d3.scaleLinear().domain([0.3, 1]).range([h, 0]);
  g.append('g').attr('class', 'grid').call(d3.axisLeft(y).ticks(5).tickSize(-w).tickFormat(''));
  g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(5).tickFormat(d3.format('.0%'))).select('.domain').remove();
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).tickValues(LEAD_TICKS).tickFormat(leadLabel).tickSizeOuter(0));
  for (const [target, key, c] of [[0.9, 'cover90', 'var(--blue)'], [0.5, 'cover50', '#7fb6ef']]) {
    g.append('line').attr('x1', 0).attr('x2', w).attr('y1', y(target)).attr('y2', y(target)).attr('stroke', c).attr('stroke-dasharray', '4 4').attr('stroke-opacity', 0.7);
    g.append('path').datum(rows).attr('d', d3.line().x((d) => x(d.lead_h)).y((d) => y(d[key]))).attr('fill', 'none').attr('stroke', c).attr('stroke-width', 2.2);
    g.selectAll(null).data(rows).join('circle').attr('cx', (d) => x(d.lead_h)).attr('cy', (d) => y(d[key])).attr('r', 2.6).attr('fill', c);
  }
  const key = g.append('g').attr('transform', 'translate(0,-12)').attr('class', 'annot');
  key.append('line').attr('x2', 16).attr('stroke', 'var(--blue)').attr('stroke-width', 2.2);
  key.append('text').attr('x', 20).attr('y', 4).text('90% range (target 90%)');
  key.append('line').attr('x1', 170).attr('x2', 186).attr('stroke', '#7fb6ef').attr('stroke-width', 2.2);
  key.append('text').attr('x', 190).attr('y', 4).text('middle 50% (target 50%)');
}

// ---------------------------------------------------------------------------------------------- snowpack

export function sweChart(el, { swe, flow }) {
  const height = 220;
  const m = { top: 20, right: 44, bottom: 26, left: 40 };
  const { g, w, h } = frame(el, height, m);
  const inch = units.temp === 'F';
  const s = swe.v.map((v, i) => ({ t: new Date((swe.t0 + i * 86400) * 1000), v: v == null ? null : inch ? v / 25.4 : v / 10 }));
  const daily = [];
  for (let i = 0; i + 24 <= flow.v.length; i += 24) {
    const chunk = flow.v.slice(i, i + 24).filter((v) => v != null);
    daily.push({ t: new Date((flow.t0 + (i + 12) * 3600) * 1000), v: chunk.length > 12 ? d3.mean(chunk) : null });
  }
  const x = d3.scaleUtc().domain(d3.extent(daily, (d) => d.t)).range([0, w]);
  const ys = d3.scaleLinear().domain([0, Math.max(inch ? 2 : 5, d3.max(s, (d) => d.v) || 0)]).nice().range([h, 0]);
  const yq = d3.scaleLog().domain(d3.extent(daily.filter((d) => d.v != null), (d) => Math.max(d.v, 0.1))).nice().range([h, 0]);
  g.append('g').attr('class', 'axis').call(d3.axisLeft(ys).ticks(4)).select('.domain').remove();
  g.append('g').attr('class', 'axis').attr('transform', `translate(${w},0)`).call(d3.axisRight(yq).ticks(4, '~s')).select('.domain').remove();
  g.append('text').attr('class', 'annot').attr('x', 0).attr('y', -6).text(`snowpack (${inch ? 'in' : 'cm'} of water)`);
  g.append('text').attr('class', 'annot').attr('x', w).attr('y', -6).attr('text-anchor', 'end').text('flow (ft³/s, log)');
  g.append('g').attr('class', 'axis').attr('transform', `translate(0,${h})`).call(d3.axisBottom(x).ticks(6).tickFormat(d3.utcFormat('%b %y')).tickSizeOuter(0));
  g.append('path').datum(s).attr('d', d3.area().defined((d) => d.v != null).x((d) => x(d.t)).y0(h).y1((d) => ys(d.v))).attr('fill', 'rgba(124,92,214,0.25)').attr('stroke', 'var(--snow)');
  g.append('path').datum(daily).attr('d', d3.line().defined((d) => d.v != null).x((d) => x(d.t)).y((d) => yq(Math.max(d.v, 0.1)))).attr('fill', 'none').attr('stroke', 'var(--blue)').attr('stroke-width', 1.3);
}
