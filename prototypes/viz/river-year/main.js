import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, fmtCfs, DAY, hiDpiCanvas, clamp } from '../shared/common.js';

mountTopbar('River Year', 'Radial climatology & 50-year spiral · Delaware at Callicoon');
mountSource('USGS NWIS daily mean discharge and water temperature, 01427510, Oct 1975 – present (recent days provisional). Percentile bands use a ±7-day window across all years except the current one.');

const daily = await loadJSON('callicoon-daily.json');
const start = Date.parse(daily.start);
const days = daily.q.map((q, i) => {
  const t = new Date(start + i * DAY);
  const y0 = Date.UTC(t.getUTCFullYear(), 0, 1);
  const doy = Math.min(364, Math.round((t - y0) / DAY));
  return { i, t, year: t.getUTCFullYear(), doy, q, wt: daily.wt[i] };
});
const years = [...new Set(days.map((d) => d.year))];
const thisYear = years[years.length - 1];
const firstFull = years[1];

const METRICS = {
  q: { key: 'q', label: 'Discharge', fmt: fmtCfs, log: true, ticks: [300, 1000, 3000, 10000, 30000], color: '#4cc9f0' },
  wt: { key: 'wt', label: 'Water temperature', fmt: (v) => (v == null ? '–' : `${v.toFixed(1)} °F`), log: false, ticks: [35, 45, 55, 65, 75], color: '#ffb703' },
};
let metric = METRICS.q;
let morph = 0; // 0 = rings, 1 = spiral
let morphTarget = 0;
let highlightYear = null;
let revealStart = performance.now();

// Per-day-of-year percentiles, ±7-day window, excluding the current year.
function climatology(key) {
  const byDoy = d3.range(365).map(() => []);
  for (const d of days) if (d[key] != null && d.year !== thisYear) byDoy[d.doy].push(d[key]);
  return d3.range(365).map((doy) => {
    const vals = [];
    for (let k = -7; k <= 7; k++) vals.push(...byDoy[(doy + k + 365) % 365]);
    vals.sort(d3.ascending);
    return { doy, min: vals[0], p10: d3.quantileSorted(vals, 0.1), p25: d3.quantileSorted(vals, 0.25), p50: d3.quantileSorted(vals, 0.5), p75: d3.quantileSorted(vals, 0.75), p90: d3.quantileSorted(vals, 0.9), max: vals[vals.length - 1], sorted: vals };
  });
}
const clim = { q: climatology('q'), wt: climatology('wt') };

// ---------- geometry ----------
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const overlay = d3.select('#overlay');
let size;
let cx, cy, R0, R1;
const angle = (doy) => (doy / 365) * Math.PI * 2 - Math.PI / 2;
let rValue;
const rTime = (d) => R0 * 0.35 + ((d.i + 0.5) / days.length) * (R1 - R0 * 0.35);

function layout() {
  const right = size.w > 1000 ? 330 : 0;
  cx = (size.w - right) / 2;
  cy = size.h / 2 + 18;
  R1 = Math.min(size.w - right, size.h) / 2 - 56;
  R0 = R1 * 0.32;
  const ext = metric.log ? [150, 40000] : [30, 85];
  rValue = (metric.log ? d3.scaleLog() : d3.scaleLinear()).domain(ext).range([R0, R1]).clamp(true);
  drawOverlay();
}

function drawOverlay() {
  overlay.selectAll('*').remove();
  const g = overlay.append('g');
  const months = d3.utcMonths(new Date(Date.UTC(2021, 0, 1)), new Date(Date.UTC(2022, 0, 1)));
  months.forEach((m) => {
    const doy = Math.round((m - Date.UTC(2021, 0, 1)) / DAY);
    const a = angle(doy);
    g.append('line').attr('x1', cx + Math.cos(a) * R0 * 0.3).attr('y1', cy + Math.sin(a) * R0 * 0.3).attr('x2', cx + Math.cos(a) * (R1 + 8)).attr('y2', cy + Math.sin(a) * (R1 + 8)).attr('stroke', 'rgba(255,255,255,0.06)');
    const am = angle(doy + 15);
    g.append('text').attr('class', 'month').attr('x', cx + Math.cos(am) * (R1 + 26)).attr('y', cy + Math.sin(am) * (R1 + 26)).attr('text-anchor', 'middle').attr('dy', '0.35em').text(d3.utcFormat('%b')(m));
  });
  const ringG = overlay.append('g').attr('class', 'rings').style('opacity', 1 - morph);
  for (const v of metric.ticks) {
    ringG.append('circle').attr('cx', cx).attr('cy', cy).attr('r', rValue(v)).attr('fill', 'none').attr('stroke', 'rgba(255,255,255,0.07)');
    ringG.append('text').attr('class', 'ring-label').attr('x', cx + 4).attr('y', cy - rValue(v) - 3).text(metric.log ? d3.format('~s')(v) : `${v}°`);
  }
  const spG = overlay.append('g').attr('class', 'spiral-labels').style('opacity', morph);
  for (const y of years.filter((y) => y % 10 === 0)) {
    const d = days.find((dd) => dd.year === y && dd.doy === 0);
    if (!d) continue;
    spG.append('text').attr('class', 'ring-label').attr('x', cx + 4).attr('y', cy - rTime(d) + 3).text(y);
  }
  overlay.append('text').attr('x', cx).attr('y', cy - 6).attr('text-anchor', 'middle').style('font-size', '28px').style('font-weight', 700).style('fill', '#e3ebf6').attr('id', 'center-year').text(highlightYear ?? thisYear);
  overlay.append('text').attr('x', cx).attr('y', cy + 16).attr('text-anchor', 'middle').style('font-size', '11px').attr('id', 'center-sub').text(metric.label.toLowerCase());
  overlay.append('circle').attr('cx', cx).attr('cy', cy).attr('r', R1 + 40).attr('fill', 'transparent').on('mousemove', hover).on('mouseleave', () => { hoverPt = null; });
}

// ---------- colors ----------
const anomalyColor = d3.scaleDiverging([-1.2, 0, 1.2], (t) => d3.interpolateRgbBasis(['#b5651d', '#e9c46a', '#1d2b45', '#4cc9f0', '#caf0f8'])(t)).clamp(true);
const tempAnomColor = d3.scaleDiverging([-6, 0, 6], (t) => d3.interpolateRgbBasis(['#4361ee', '#4cc9f0', '#1d2b45', '#ffb703', '#fb5607'])(t)).clamp(true);
function anomaly(d) {
  const v = d[metric.key];
  if (v == null) return null;
  const c = clim[metric.key][d.doy];
  return metric.log ? Math.log(v / c.p50) : v - c.p50;
}
function drawLegend() {
  const c = document.getElementById('legend').getContext('2d');
  const sc = metric.log ? anomalyColor : tempAnomColor;
  const dom = metric.log ? [-1.2, 1.2] : [-6, 6];
  for (let k = 0; k < 150; k++) { c.fillStyle = sc(dom[0] + (k / 149) * (dom[1] - dom[0])); c.fillRect(k, 0, 1, 8); }
  document.getElementById('lg-lo').textContent = metric.log ? '⅓× median' : '−6 °F';
  document.getElementById('lg-hi').textContent = metric.log ? '3× median' : '+6 °F';
  document.getElementById('explain').innerHTML = morphTarget === 0
    ? `Angle is the day of the year; distance from the center is ${metric.label.toLowerCase()}${metric.log ? ' (log)' : ''}. Bands show the historical 10–90% and 25–75% range, white is the median, faint lines are the past ${years.length - 1} years, and the glowing line is ${thisYear}.`
    : `Every day since Oct 1975 sits on one continuous spiral: the angle is the day of the year and the radius is time (the center is 1975, the rim is today). Color is the departure from that day's median, so wet years glow cyan and droughts turn amber.`;
}

// ---------- rendering ----------
function pos(d, v) {
  const a = angle(d.doy);
  const r = (1 - morph) * rValue(v) + morph * rTime(d);
  return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
}

function render(now) {
  const { w, h } = size;
  ctx.clearRect(0, 0, w, h);
  const key = metric.key;
  const C = clim[key];
  const ringA = 1 - morph;

  if (ringA > 0.01) {
    // percentile bands
    const band = (lo, hi, fill) => {
      ctx.beginPath();
      for (let k = 0; k <= 365; k++) { const a = angle(k); const r = rValue(C[k % 365][hi]); k ? ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : ctx.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); }
      for (let k = 365; k >= 0; k--) { const a = angle(k); const r = rValue(C[k % 365][lo]); ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); }
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    };
    ctx.globalAlpha = ringA;
    band('min', 'max', 'rgba(76,201,240,0.05)');
    band('p10', 'p90', 'rgba(76,201,240,0.10)');
    band('p25', 'p75', 'rgba(76,201,240,0.16)');
    // past years
    ctx.lineWidth = 0.6;
    for (const y of years) {
      if (y === thisYear) continue;
      const pts = days.filter((d) => d.year === y && d[key] != null);
      ctx.strokeStyle = y === highlightYear ? 'rgba(255,209,102,0.95)' : `rgba(160,190,255,${0.05 + 0.1 * ((y - years[0]) / years.length)})`;
      ctx.lineWidth = y === highlightYear ? 2 : 0.6;
      ctx.beginPath();
      pts.forEach((d, k) => { const [x, yy] = pos(d, d[key]); k && pts[k - 1].i === d.i - 1 ? ctx.lineTo(x, yy) : ctx.moveTo(x, yy); });
      ctx.stroke();
    }
    // median
    ctx.beginPath();
    C.forEach((c, k) => { const a = angle(k); const r = rValue(c.p50); k ? ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : ctx.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); });
    ctx.closePath();
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }

  if (morph > 0.01) {
    // anomaly-colored spiral of every day
    const sc = metric.log ? anomalyColor : tempAnomColor;
    ctx.globalAlpha = morph;
    const dotR = Math.max(1.2, ((R1 - R0 * 0.35) / years.length) * 0.55);
    for (const d of days) {
      const an = anomaly(d);
      if (an == null) continue;
      const [x, y] = pos(d, d[key]);
      ctx.fillStyle = sc(an);
      ctx.fillRect(x - dotR / 2, y - dotR / 2, dotR, dotR);
    }
    if (highlightYear) {
      ctx.strokeStyle = '#ffd166';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      days.filter((d) => d.year === highlightYear).forEach((d, k) => { const [x, y] = pos(d, d[key]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // current year, revealed progressively
  const cur = days.filter((d) => d.year === thisYear && d[key] != null);
  const n = Math.floor(clamp((now - revealStart) / 5000, 0, 1) * cur.length);
  if (n > 1) {
    ctx.save();
    ctx.shadowColor = metric.color;
    ctx.shadowBlur = 14;
    ctx.lineWidth = 2.6;
    ctx.lineCap = 'round';
    for (let k = 1; k < n; k++) {
      const a = cur[k - 1];
      const b = cur[k];
      const [x0, y0] = pos(a, a[key]);
      const [x1, y1] = pos(b, b[key]);
      const an = anomaly(b);
      ctx.strokeStyle = morph > 0.5 ? '#ffffff' : (metric.log ? anomalyColor(an) : tempAnomColor(an));
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }
    const head = cur[n - 1];
    const [hx, hy] = pos(head, head[key]);
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.arc(hx, hy, 4 + Math.sin(now / 200) * 1.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  if (hoverPt) {
    const [x, y] = pos(hoverPt, hoverPt[key]);
    ctx.strokeStyle = '#ffd166';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.stroke();
  }
}

// ---------- hover ----------
let hoverPt = null;
function hover(event) {
  const [mx, my] = d3.pointer(event, overlay.node());
  let a = Math.atan2(my - cy, mx - cx) + Math.PI / 2;
  if (a < 0) a += Math.PI * 2;
  const doy = clamp(Math.round((a / (Math.PI * 2)) * 365), 0, 364);
  const r = Math.hypot(mx - cx, my - cy);
  const key = metric.key;
  let best = null;
  if (morph < 0.5) {
    const y = highlightYear ?? thisYear;
    best = days.find((d) => d.year === y && d.doy === doy && d[key] != null);
    // Otherwise pick the year whose value is nearest the cursor radius
    let bestDist = best ? Math.abs(rValue(best[key]) - r) : Infinity;
    if (bestDist > 10) {
      for (const d of days) if (d.doy === doy && d[key] != null) { const dd = Math.abs(rValue(d[key]) - r); if (dd < bestDist) { bestDist = dd; best = d; } }
    }
  } else {
    const t = ((r - R0 * 0.35) / (R1 - R0 * 0.35)) * days.length;
    let bestDist = Infinity;
    for (const d of days) if (d.doy === doy) { const dd = Math.abs(d.i - t); if (dd < bestDist) { bestDist = dd; best = d; } }
  }
  hoverPt = best;
  if (!best) return;
  const c = clim[key][best.doy];
  const v = best[key];
  const rank = v == null ? null : d3.bisectLeft(c.sorted, v) / c.sorted.length;
  document.getElementById('hover-date').textContent = d3.utcFormat('%A, %B %-d, %Y')(best.t);
  document.getElementById('hover-val').textContent = metric.fmt(v);
  document.getElementById('hover-rank').textContent = rank == null ? 'no data' : `${d3.format('.0%')(rank)} percentile for this date · median ${metric.fmt(c.p50)}`;
}

// ---------- controls ----------
const setView = (m) => {
  morphTarget = m;
  document.getElementById('v-rings').classList.toggle('active', m === 0);
  document.getElementById('v-spiral').classList.toggle('active', m === 1);
  document.getElementById('legend-row').style.visibility = 'visible';
  drawLegend();
};
document.getElementById('v-rings').onclick = () => setView(0);
document.getElementById('v-spiral').onclick = () => setView(1);
document.getElementById('replay').onclick = () => { revealStart = performance.now(); };
const setMetric = (k) => {
  metric = METRICS[k];
  document.getElementById('m-q').classList.toggle('active', k === 'q');
  document.getElementById('m-wt').classList.toggle('active', k === 'wt');
  revealStart = performance.now();
  layout();
  drawLegend();
};
document.getElementById('m-q').onclick = () => setMetric('q');
document.getElementById('m-wt').onclick = () => setMetric('wt');
d3.select('#years').selectAll('button').data([...years].reverse()).join('button').text((y) => y)
  .on('click', function (_, y) {
    highlightYear = highlightYear === y ? null : y;
    d3.selectAll('#years button').classed('active', (yy) => yy === highlightYear);
    d3.select('#center-year').text(highlightYear ?? thisYear);
  });

size = hiDpiCanvas(canvas, () => size && layout());
layout();
const params = new URLSearchParams(location.search);
if (params.get('view') === 'spiral') { setView(1); morph = 1; }
if (params.get('metric') === 'wt') setMetric('wt');
drawLegend();

let lastT = performance.now();
function frame(now) {
  const dt = (now - lastT) / 1000;
  lastT = now;
  if (Math.abs(morph - morphTarget) > 0.001) {
    morph += (morphTarget - morph) * Math.min(1, dt * 3.2);
    overlay.select('.rings').style('opacity', 1 - morph);
    overlay.select('.spiral-labels').style('opacity', morph);
  }
  render(now);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
