import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, tooltip, fmtCfs, fmtET, HOUR, DAY, clamp } from '../shared/common.js';

mountTopbar('Storm Explorer', 'Hyetograph + hydrograph with linked brushing');
mountSource('Flow & water temp: USGS NWIS instantaneous values, 01427510 (hourly means, provisional). Weather: Open-Meteo historical API (ECMWF IFS 9 km analysis), mean of 42 points in the basin. Drainage area 1,820 sq mi (NWIS).');

const [cal, wx] = await Promise.all([loadJSON('callicoon-hourly.json'), loadJSON('weather-basin-hourly.json')]);
const tip = tooltip();
const AREA_FT2 = 1820 * 27878400;

// ---------- align to one hourly table ----------
const c0 = Date.parse(cal.start);
const w0 = Date.parse(wx.start);
const t0 = Math.max(c0, w0);
const t1 = Math.min(c0 + cal.length * HOUR, w0 + wx.length * HOUR);
const N = Math.round((t1 - t0) / HOUR);
const rows = d3.range(N).map((i) => {
  const t = t0 + i * HOUR;
  const ci = Math.round((t - c0) / HOUR);
  const wi = Math.round((t - w0) / HOUR);
  return {
    i,
    t: new Date(t),
    q: cal.q[ci],
    wt: cal.wt[ci],
    p: wx.precipitation[wi] ?? 0,
    snowCm: wx.snowfall[wi] ?? 0,
    depth: wx.snow_depth[wi] ?? 0,
    air: wx.temperature_2m[wi],
    sw: wx.shortwave_radiation[wi],
  };
});
// Fill small gaps in discharge by linear interpolation (≤ 12 h) for the filter only.
const qFilled = rows.map((r) => r.q);
for (let i = 0; i < N; i++) {
  if (qFilled[i] != null) continue;
  let j = i;
  while (j < N && qFilled[j] == null) j++;
  const a = qFilled[i - 1];
  const b = qFilled[j];
  for (let k = i; k < j; k++) qFilled[k] = a != null && b != null && j - i <= 12 ? a + ((b - a) * (k - i + 1)) / (j - i + 1) : a ?? b;
  i = j;
}
// Lyne–Hollick baseflow filter, forward/backward/forward.
function lyneHollick(q, alpha = 0.925, passes = 3) {
  let base = q.slice();
  for (let p = 0; p < passes; p++) {
    const src = p % 2 === 0 ? base : base.slice().reverse();
    const out = new Array(src.length);
    let qf = 0;
    out[0] = src[0];
    for (let i = 1; i < src.length; i++) {
      qf = alpha * qf + ((1 + alpha) / 2) * (src[i] - src[i - 1]);
      if (qf < 0) qf = 0;
      out[i] = Math.min(src[i], src[i] - qf);
    }
    base = p % 2 === 0 ? out : out.reverse();
  }
  return base;
}
const baseflow = lyneHollick(qFilled);
rows.forEach((r, i) => { r.base = r.q != null ? Math.min(r.q, baseflow[i]) : null; r.quick = r.q != null ? Math.max(0, r.q - baseflow[i]) : null; });

// ---------- event detection ----------
function detectEvents() {
  const peaks = [];
  const win = 72;
  for (let i = win; i < N - win; i++) {
    const q = rows[i].q;
    if (q == null) continue;
    let isMax = true;
    for (let k = i - win; k <= i + win; k++) if (rows[k].q != null && rows[k].q > q) { isMax = false; break; }
    if (isMax && rows[i].quick > 0.35 * q && (!peaks.length || i - peaks[peaks.length - 1] > win)) peaks.push(i);
  }
  return peaks
    .map((pi) => eventStats(Math.max(0, pi - 84), Math.min(N - 1, pi + 120)))
    .filter((e, k, all) => e.peak && all.findIndex((o) => o.peak?.t.getTime() === e.peak.t.getTime()) === k)
    .sort((a, b) => b.peak.q - a.peak.q);
}
function eventStats(i0, i1) {
  const sel = rows.slice(i0, i1 + 1);
  const rain = d3.sum(sel, (r) => r.p);
  const peak = d3.greatest(sel.filter((r) => r.q != null), (r) => r.q);
  // Rain centroid over the part of the window before the peak
  const pre = sel.filter((r) => peak && r.t <= peak.t);
  const pSum = d3.sum(pre, (r) => r.p);
  const centroid = pSum > 0.02 ? d3.sum(pre, (r) => r.p * r.t.getTime()) / pSum : null;
  const qfVol = d3.sum(sel, (r) => (r.quick ?? 0) * 3600);
  const qfDepth = (qfVol / AREA_FT2) * 12;
  const meltIn = d3.sum(sel, (r, k) => (k > 0 ? Math.max(0, sel[k - 1].depth - r.depth) : 0)) * 39.37 * 0.25;
  return { i0, i1, rain, peak, lagH: centroid && peak ? (peak.t - centroid) / HOUR : null, qfDepth, ratio: rain > 0.05 ? qfDepth / rain : null, meltIn };
}
const events = detectEvents().slice(0, 9);

// ---------- layout ----------
const focusSvg = d3.select('#focus svg');
const ctxSvg = d3.select('#context svg');
let logScale = false;
let domain = [rows[0].t, rows[N - 1].t];
let selectedEvent = null;

const x = d3.scaleUtc();
const xCtx = d3.scaleUtc();
const brush = d3.brushX().on('brush end', brushed);
let brushG;

function brushed({ selection, sourceEvent }) {
  if (!selection) return;
  domain = selection.map((v) => xCtx.invert(v));
  if (sourceEvent) selectedEvent = null;
  drawFocus();
  updateStats();
  drawScatter();
}

function drawContext() {
  const node = ctxSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 50, r: 50, t: 6, b: 18 };
  xCtx.domain([rows[0].t, rows[N - 1].t]).range([m.l, w - m.r]);
  const y = d3.scaleLog([Math.max(100, d3.min(rows, (r) => r.q)), d3.max(rows, (r) => r.q)], [h - m.b, m.t + 10]);
  const daily = d3.rollups(rows, (v) => d3.sum(v, (r) => r.p), (r) => d3.utcDay.floor(r.t)).map(([t, p]) => ({ t, p }));
  const yp = d3.scaleLinear([0, d3.max(daily, (d) => d.p)], [m.t, (h - m.b) * 0.6]);
  ctxSvg.selectAll('*').remove();
  ctxSvg.append('g').selectAll('rect').data(daily).join('rect').attr('x', (d) => xCtx(d.t)).attr('width', Math.max(1, (w - m.l - m.r) / daily.length - 0.4)).attr('y', m.t).attr('height', (d) => yp(d.p) - m.t).attr('fill', 'rgba(123,140,255,0.6)');
  ctxSvg.append('path').attr('d', d3.line().defined((r) => r.q != null).x((r) => xCtx(r.t)).y((r) => y(r.q))(rows)).attr('fill', 'none').attr('stroke', '#4cc9f0').attr('stroke-width', 1);
  ctxSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(xCtx).ticks(12).tickSizeOuter(0));
  ctxSvg.selectAll('.ev').data(events).join('text').attr('class', 'ev').attr('x', (e) => xCtx(e.peak.t)).attr('y', m.t + 9).attr('text-anchor', 'middle').style('font-size', '10px').style('fill', '#ffb703').text((_, k) => k + 1);
  brush.extent([[m.l, m.t], [w - m.r, h - m.b]]);
  brushG = ctxSvg.append('g').attr('class', 'brush').call(brush);
  brushG.call(brush.move, domain.map(xCtx));
}

const PANELS = [
  { key: 'hyeto', title: 'Basin precipitation (in/h) · rain vs snow · cumulative', frac: 0.24 },
  { key: 'hydro', title: 'Discharge at Callicoon (cfs) · baseflow (dark) vs quickflow', frac: 0.36 },
  { key: 'temp', title: 'Water temperature vs air temperature (°F)', frac: 0.2 },
  { key: 'snow', title: 'Snow depth (in) · shortwave radiation (W/m²)', frac: 0.2 },
];

function drawFocus() {
  const node = focusSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 50, r: 50, t: 4, b: 20 };
  const gap = 14;
  x.domain(domain).range([m.l, w - m.r]);
  const sel = rows.filter((r) => r.t >= domain[0] && r.t <= domain[1]);
  const hoursSpan = (domain[1] - domain[0]) / HOUR;
  const agg = hoursSpan > 24 * 60 ? 24 : hoursSpan > 24 * 20 ? 6 : 1;
  const binned = d3.rollups(sel, (v) => ({ p: d3.sum(v, (r) => r.p), snow: d3.sum(v, (r) => r.snowCm) }), (r) => Math.floor(r.i / agg)).map(([k, v]) => ({ t: new Date(t0 + k * agg * HOUR), ...v }));
  focusSvg.selectAll('*').remove();
  let top = m.t;
  const avail = h - m.t - m.b - gap * (PANELS.length - 1);
  const ys = {};
  for (const panel of PANELS) {
    const ph = avail * panel.frac;
    const g = focusSvg.append('g');
    const y0 = top;
    const y1 = top + ph;
    ys[panel.key] = [y0, y1];
    g.append('rect').attr('x', m.l).attr('y', y0).attr('width', w - m.l - m.r).attr('height', ph).attr('fill', 'rgba(255,255,255,0.015)');
    if (panel.key === 'hyeto') {
      const y = d3.scaleLinear([0, Math.max(0.02, d3.max(binned, (d) => d.p))], [y0, y1]);
      const bw = Math.max(1, x(new Date(t0 + agg * HOUR)) - x(new Date(t0)) - 0.5);
      g.selectAll('rect.b').data(binned).join('rect').attr('class', 'b')
        .attr('x', (d) => x(d.t)).attr('y', y0).attr('width', bw).attr('height', (d) => y(d.p) - y0)
        .attr('fill', (d) => (d.snow > 0.05 && d.snow * 0.1 / 2.54 > d.p * 0.4 ? '#f1f5ff' : '#7b8cff'));
      let acc = 0;
      const cum = sel.map((r) => ({ t: r.t, c: (acc += r.p) }));
      const yc = d3.scaleLinear([0, Math.max(0.1, acc)], [y1, y0 + 4]);
      g.append('path').attr('d', d3.line().x((d) => x(d.t)).y((d) => yc(d.c))(cum)).attr('fill', 'none').attr('stroke', '#ffd166').attr('stroke-width', 1.4);
      g.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3));
      g.append('g').attr('class', 'axis').attr('transform', `translate(${w - m.r},0)`).call(d3.axisRight(yc).ticks(3));
      g.append('text').attr('x', w - m.r - 4).attr('y', y1 - 4).attr('text-anchor', 'end').style('font-size', '10px').style('fill', '#ffd166').text(`${acc.toFixed(2)} in total`);
    }
    if (panel.key === 'hydro') {
      const qs = sel.filter((r) => r.q != null);
      const y = (logScale ? d3.scaleLog().domain([Math.max(50, d3.min(qs, (r) => r.base) * 0.9), d3.max(qs, (r) => r.q) * 1.08]) : d3.scaleLinear().domain([0, d3.max(qs, (r) => r.q) * 1.08])).range([y1, y0]);
      if (!logScale) y.nice();
      const defined = (r) => r.q != null;
      g.append('path').attr('d', d3.area().defined(defined).x((r) => x(r.t)).y0(y1).y1((r) => y(Math.max(r.base, y.domain()[0])))(sel)).attr('fill', 'rgba(30,70,140,0.7)');
      g.append('path').attr('d', d3.area().defined(defined).x((r) => x(r.t)).y0((r) => y(Math.max(r.base, y.domain()[0]))).y1((r) => y(r.q))(sel)).attr('fill', 'rgba(76,201,240,0.45)');
      g.append('path').attr('d', d3.line().defined(defined).x((r) => x(r.t)).y((r) => y(r.q))(sel)).attr('fill', 'none').attr('stroke', '#4cc9f0').attr('stroke-width', 1.5);
      const pk = d3.greatest(qs, (r) => r.q);
      if (pk) {
        g.append('circle').attr('cx', x(pk.t)).attr('cy', y(pk.q)).attr('r', 4).attr('fill', '#ffb703');
        g.append('text').attr('x', x(pk.t) + 7).attr('y', y(pk.q) + 4).style('font-size', '11px').style('fill', '#ffb703').text(fmtCfs(pk.q));
      }
      g.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4, '~s'));
    }
    if (panel.key === 'temp') {
      const vals = sel.flatMap((r) => [r.wt, r.air]).filter((v) => v != null);
      const y = d3.scaleLinear(d3.extent(vals), [y1, y0]).nice();
      g.append('path').attr('d', d3.line().defined((r) => r.air != null).x((r) => x(r.t)).y((r) => y(r.air))(sel)).attr('fill', 'none').attr('stroke', 'rgba(255,183,3,0.55)').attr('stroke-width', 1);
      g.append('path').attr('d', d3.line().defined((r) => r.wt != null).x((r) => x(r.t)).y((r) => y(r.wt))(sel)).attr('fill', 'none').attr('stroke', '#fb5607').attr('stroke-width', 1.8);
      if (y.domain()[0] < 32 && y.domain()[1] > 32) g.append('line').attr('x1', m.l).attr('x2', w - m.r).attr('y1', y(32)).attr('y2', y(32)).attr('stroke', 'rgba(241,245,255,0.3)').attr('stroke-dasharray', '3 3');
      g.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3));
    }
    if (panel.key === 'snow') {
      const y = d3.scaleLinear([0, Math.max(2, d3.max(sel, (r) => r.depth * 39.37))], [y1, y0]).nice();
      const yr = d3.scaleLinear([0, Math.max(200, d3.max(sel, (r) => r.sw))], [y1, y0]);
      g.append('path').attr('d', d3.area().x((r) => x(r.t)).y0(y1).y1((r) => y(r.depth * 39.37))(sel)).attr('fill', 'rgba(241,245,255,0.25)').attr('stroke', 'rgba(241,245,255,0.6)');
      const swLine = agg > 1 ? d3.rollups(sel, (v) => d3.mean(v, (r) => r.sw), (r) => Math.floor(r.i / 24)).map(([k, v]) => ({ t: new Date(t0 + (k * 24 + 12) * HOUR), sw: v })) : sel;
      g.append('path').attr('d', d3.line().defined((r) => r.sw != null).x((r) => x(r.t)).y((r) => yr(r.sw))(swLine)).attr('fill', 'none').attr('stroke', 'rgba(255,209,102,0.7)').attr('stroke-width', 1);
      g.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3));
      g.append('g').attr('class', 'axis').attr('transform', `translate(${w - m.r},0)`).call(d3.axisRight(yr).ticks(3));
    }
    g.append('text').attr('class', 'panel-title').attr('x', m.l + 6).attr('y', y0 + 12).text(panel.title);
    top = y1 + gap;
  }
  focusSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(w / 120).tickSizeOuter(0));
  // Crosshair shared by all panels
  const xh = focusSvg.append('line').attr('class', 'xhair').attr('y1', m.t).attr('y2', h - m.b).style('opacity', 0);
  focusSvg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', w - m.l - m.r).attr('height', h - m.t - m.b).attr('fill', 'transparent')
    .on('mousemove', (event) => {
      const t = x.invert(d3.pointer(event)[0]);
      const r = rows[clamp(Math.round((t - t0) / HOUR), 0, N - 1)];
      xh.attr('x1', x(r.t)).attr('x2', x(r.t)).style('opacity', 1);
      tip.show(`<b>${fmtET(r.t)} ET</b><br>Discharge <span class="num">${fmtCfs(r.q)}</span> (baseflow ${fmtCfs(r.base)})<br>Precip <span class="num">${r.p.toFixed(3)} in/h</span>${r.snowCm > 0 ? ` · snow ${r.snowCm.toFixed(1)} cm` : ''}<br>Water <span class="num">${r.wt?.toFixed(1) ?? '–'} °F</span> · air <span class="num">${r.air?.toFixed(1) ?? '–'} °F</span><br>Snow depth <span class="num">${(r.depth * 39.37).toFixed(1)} in</span> · sun <span class="num">${r.sw?.toFixed(0) ?? '–'} W/m²</span>`, event);
    })
    .on('mouseleave', () => { xh.style('opacity', 0); tip.hide(); });
}

function updateStats() {
  const i0 = clamp(Math.round((domain[0] - t0) / HOUR), 0, N - 1);
  const i1 = clamp(Math.round((domain[1] - t0) / HOUR), 0, N - 1);
  const s = eventStats(i0, i1);
  document.getElementById('range').textContent = `${d3.utcFormat('%b %-d, %Y')(domain[0])} – ${d3.utcFormat('%b %-d, %Y')(domain[1])}`;
  document.getElementById('s-rain').textContent = `${s.rain.toFixed(2)} in`;
  document.getElementById('s-peak').textContent = s.peak ? fmtCfs(s.peak.q) : '–';
  document.getElementById('s-lag').textContent = s.lagH != null && (i1 - i0) < 24 * 20 ? `${s.lagH.toFixed(0)} h` : '–';
  document.getElementById('s-ratio').textContent = s.ratio != null ? `${(s.ratio * 100).toFixed(0)}%` : '–';
  document.getElementById('s-qf').textContent = `${s.qfDepth.toFixed(2)} in`;
  document.getElementById('s-melt').textContent = s.meltIn > 0.05 ? `~${s.meltIn.toFixed(1)} in SWE` : 'none';
}

// ---------- scatter ----------
const scSvg = d3.select('#scatter svg');
const allEvents = detectEvents();
function drawScatter() {
  const node = scSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 40, r: 10, t: 12, b: 32 };
  const x = d3.scaleLinear([0, d3.max(allEvents, (e) => e.rain) * 1.1], [m.l, w - m.r]).nice();
  const y = d3.scaleLinear([0, d3.max(allEvents, (e) => e.qfDepth) * 1.1], [h - m.b, m.t]).nice();
  const r = d3.scaleSqrt([0, d3.max(allEvents, (e) => e.peak.q)], [2, 14]);
  scSvg.selectAll('*').remove();
  scSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(5));
  scSvg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5));
  scSvg.append('text').attr('x', w - m.r).attr('y', h - 4).attr('text-anchor', 'end').style('font-size', '10.5px').text('basin precip in event window (in)');
  scSvg.append('text').attr('transform', `translate(12,${m.t}) rotate(-90)`).attr('text-anchor', 'end').style('font-size', '10.5px').text('quickflow depth (in)');
  for (const k of [0.25, 0.5]) {
    const xm = Math.min(x.domain()[1], y.domain()[1] / k);
    scSvg.append('line').attr('x1', x(0)).attr('y1', y(0)).attr('x2', x(xm)).attr('y2', y(xm * k)).attr('stroke', 'rgba(255,255,255,0.15)').attr('stroke-dasharray', '3 3');
    scSvg.append('text').attr('x', x(xm) + 3).attr('y', y(xm * k) + (k === 0.5 ? 10 : -3)).attr('text-anchor', k === 0.5 ? 'start' : 'end').style('font-size', '9.5px').text(`${k * 100}% runoff`);
  }
  const inView = (e) => e.peak.t >= domain[0] && e.peak.t <= domain[1];
  scSvg.append('g').selectAll('circle').data(allEvents).join('circle')
    .attr('cx', (e) => x(e.rain)).attr('cy', (e) => y(e.qfDepth)).attr('r', (e) => r(e.peak.q))
    .attr('fill', (e) => (e === selectedEvent ? '#ffb703' : inView(e) ? '#4cc9f0' : 'rgba(76,201,240,0.25)'))
    .attr('stroke', (e) => (e === selectedEvent ? '#fff' : 'none'))
    .style('cursor', 'pointer')
    .on('mousemove', (event, e) => tip.show(`<b>${d3.utcFormat('%b %-d, %Y')(e.peak.t)}</b><br>Peak ${fmtCfs(e.peak.q)}<br>Precip ${e.rain.toFixed(2)} in → quickflow ${e.qfDepth.toFixed(2)} in (${e.ratio ? (e.ratio * 100).toFixed(0) : '–'}%)<br>Lag ${e.lagH?.toFixed(0) ?? '–'} h`, event))
    .on('mouseleave', () => tip.hide())
    .on('click', (_, e) => focusEvent(e));
}

function focusEvent(e) {
  selectedEvent = e;
  brushG.transition().duration(1100).ease(d3.easeCubicInOut).call(brush.move, [rows[e.i0].t, rows[e.i1].t].map(xCtx));
  d3.selectAll('#chips button').classed('active', (d) => d === e);
}

d3.select('#chips').selectAll('button').data(events).join('button')
  .html((e, k) => `<span style="color:#ffb703">${k + 1}</span> ${d3.utcFormat('%b %-d')(e.peak.t)} · ${d3.format('.2~s')(e.peak.q)}`)
  .on('click', (_, e) => focusEvent(e));
document.getElementById('log').onclick = (ev) => { logScale = !logScale; ev.target.classList.toggle('active', logScale); drawFocus(); };
document.getElementById('all').onclick = () => {
  selectedEvent = null;
  d3.selectAll('#chips button').classed('active', false);
  brushG.transition().duration(1100).call(brush.move, [rows[0].t, rows[N - 1].t].map(xCtx));
};

function drawAll() { drawContext(); drawFocus(); updateStats(); drawScatter(); }
drawAll();
window.addEventListener('resize', drawAll);
const params = new URLSearchParams(location.search);
setTimeout(() => focusEvent(events[clamp(+(params.get('event') ?? 1) - 1, 0, events.length - 1)]), 700);
