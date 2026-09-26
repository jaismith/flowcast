import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, tooltip, fmtCfs, fmtF, fmtET, hiDpiCanvas, HOUR, clamp } from '../shared/common.js';
import { buildNetwork, snapGauge, SQMI_TO_SQKM } from '../shared/network.js';

mountTopbar('River Pulse', 'Animated D3 network map · upper Delaware above Callicoon');
mountSource('Data: USGS NWIS instantaneous values (27 gauges), NHDPlus V2 flowlines & waterbodies via USGS GeoServer, basin from USGS NLDI, basin rain from Open-Meteo (ECMWF IFS 9 km). Provisional data.');

const MAX_PARTICLES = 26000;

const [basin, rivers, waterbodies, weather] = await Promise.all([
  loadJSON('basin.json'),
  loadJSON('rivers.json'),
  loadJSON('waterbodies.json'),
  loadJSON('weather-basin-hourly.json'),
]);
const gaugeSets = { storm: await loadJSON('gauges-storm.json'), recent: null };

const tip = tooltip();
const baseCanvas = document.getElementById('base');
const pCanvas = document.getElementById('particles');
const overlay = d3.select('#overlay');
const pctx = pCanvas.getContext('2d');

// ---------- network ----------
const segs = buildNetwork(rivers);

let state = null; // per-window derived data

function prepareWindow(set) {
  const gauges = set.sites
    .filter((g) => g.drainageSqMi)
    .map((g) => ({ ...g, areaKm: g.drainageSqMi * SQMI_TO_SQKM, t0: Date.parse(set.start) }));
  for (const g of gauges) {
    g.seg = snapGauge(segs, g);
    g.meanQ = d3.mean(g.q.filter((v) => v != null));
  }
  const gaugeAt = new Map();
  for (const g of gauges) if (g.seg && (!gaugeAt.has(g.seg) || gaugeAt.get(g.seg).areaKm < g.areaKm)) gaugeAt.set(g.seg, g);
  // Each reach is controlled by the chain of gauges met walking downstream.
  for (const s of segs) {
    s.chain = [];
    for (let cur = s; cur && s.chain.length < 4; cur = cur.down) {
      const g = gaugeAt.get(cur);
      if (g) s.chain.push(g);
    }
  }
  const outlet = gauges.find((g) => g.id === '01427510');
  const wx0 = Date.parse(weather.start);
  return { set, gauges, outlet, t0: Date.parse(set.start), n: set.length, wx0, flow: new Float32Array(segs.length), wt: new Float32Array(segs.length) };
}

function gaugeValue(g, key, idx) {
  const arr = g[key];
  if (!arr) return null;
  const i0 = Math.floor(idx);
  const a = arr[clamp(i0, 0, arr.length - 1)];
  const b = arr[clamp(i0 + 1, 0, arr.length - 1)];
  if (a == null || b == null) return a ?? b;
  return a + (b - a) * (idx - i0);
}

function updateFlows(idx) {
  const { flow, wt } = state;
  for (const s of segs) {
    let q = null;
    let temp = null;
    for (const g of s.chain) {
      if (q == null) {
        const v = gaugeValue(g, 'q', idx);
        if (v != null) q = v * (s.areaSqKm / g.seg.areaSqKm);
      }
      if (temp == null) temp = gaugeValue(g, 'wt', idx);
      if (q != null && temp != null) break;
    }
    flow[s.i] = q ?? s.meanFlowCfs;
    wt[s.i] = temp ?? NaN;
  }
}

// ---------- projection & static layer ----------
let projection;
let size;
const geo = { pts: [], cum: [], len: new Float32Array(segs.length) };

function layout() {
  const w = size.w;
  const h = size.h;
  const right = w > 900 ? 300 : 20;
  projection = d3.geoMercator().fitExtent([[40, 70], [w - right - 20, h - 170]], basin.features[0]);
  segs.forEach((s) => {
    const pts = s.coords.map((c) => projection(c));
    const cum = [0];
    for (let k = 1; k < pts.length; k++) cum.push(cum[k - 1] + Math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]));
    geo.pts[s.i] = pts;
    geo.cum[s.i] = cum;
    geo.len[s.i] = cum[cum.length - 1] || 0.01;
  });
  drawBase();
  drawOverlayStatic();
  pctx.clearRect(0, 0, w, h);
}

function drawBase() {
  const ctx = baseCanvas.getContext('2d');
  const { w, h } = size;
  ctx.clearRect(0, 0, w, h);
  const bg = ctx.createRadialGradient(w * 0.45, h * 0.4, 50, w * 0.45, h * 0.45, Math.max(w, h) * 0.75);
  bg.addColorStop(0, '#0b1528');
  bg.addColorStop(1, '#04070e');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, w, h);
  const path = d3.geoPath(projection, ctx);
  ctx.save();
  ctx.shadowColor = 'rgba(76, 201, 240, 0.35)';
  ctx.shadowBlur = 30;
  ctx.beginPath();
  path(basin);
  ctx.fillStyle = '#0a1426';
  ctx.fill();
  ctx.restore();
  ctx.beginPath();
  path(basin);
  ctx.strokeStyle = 'rgba(128, 255, 219, 0.35)';
  ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  path(waterbodies);
  ctx.fillStyle = 'rgba(40, 110, 170, 0.55)';
  ctx.fill();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const s of segs) {
    const pts = geo.pts[s.i];
    ctx.beginPath();
    pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = `rgba(76, 150, 220, ${0.18 + s.order * 0.06})`;
    ctx.lineWidth = 0.3 + Math.sqrt(s.meanFlowCfs) / 16;
    ctx.stroke();
  }
}

function drawOverlayStatic() {
  overlay.selectAll('*').remove();
  const path = d3.geoPath(projection);
  // River labels at the longest-named reach groups
  const byName = d3.group(segs.filter((s) => s.name && s.order >= 3), (s) => s.name);
  const labels = [];
  for (const [name, list] of byName) {
    const total = d3.sum(list, (s) => s.lengthKm);
    if (total < 18) continue;
    const mid = list.sort((a, b) => a.hydroseq - b.hydroseq)[Math.floor(list.length / 2)];
    const pts = geo.pts[mid.i];
    const a = pts[0];
    const b = pts[pts.length - 1];
    let ang = (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI;
    if (ang > 90) ang -= 180;
    if (ang < -90) ang += 180;
    labels.push({ name, x: pts[Math.floor(pts.length / 2)][0], y: pts[Math.floor(pts.length / 2)][1], ang });
  }
  overlay.append('g').selectAll('text').data(labels).join('text')
    .attr('class', 'river-label')
    .attr('transform', (d) => `translate(${d.x},${d.y - 6}) rotate(${d.ang})`)
    .attr('text-anchor', 'middle')
    .text((d) => d.name);
  const res = waterbodies.features.filter((f) => f.properties.name && f.properties.areaSqKm > 5);
  overlay.append('g').selectAll('text').data(res).join('text')
    .attr('class', 'res-label')
    .attr('x', (d) => path.centroid(d)[0])
    .attr('y', (d) => path.centroid(d)[1] + 18)
    .attr('text-anchor', 'middle')
    .text((d) => d.properties.name);
  overlay.append('g').attr('class', 'gauges');
  bindGauges();
}

function bindGauges() {
  const g = overlay.select('g.gauges').selectAll('g.gauge').data(state.gauges.filter((d) => d.seg), (d) => d.id);
  const enter = g.enter().append('g').attr('class', 'gauge');
  enter.append('circle').attr('class', 'halo').attr('fill', 'none').attr('stroke', '#80ffdb').attr('stroke-opacity', 0.5);
  enter.append('circle').attr('class', 'dot').attr('fill', '#0b1222').attr('stroke', '#80ffdb').attr('stroke-width', 1.5);
  g.exit().remove();
  overlay.selectAll('g.gauge')
    .attr('transform', (d) => `translate(${projection([d.lon, d.lat])})`)
    .on('mousemove', (event, d) => showGauge(event, d))
    .on('mouseleave', () => { tip.hide(); hovered = null; drawTimeline(); });
  overlay.selectAll('g.gauge').filter((d) => d.id === '01427510').select('.dot').attr('fill', '#80ffdb');
}

let hovered = null;
function showGauge(event, d) {
  hovered = d;
  const idx = (clock - state.t0) / HOUR;
  const vals = d.q.map((v, i) => [i, v]).filter(([, v]) => v != null);
  const x = d3.scaleLinear([0, d.q.length - 1], [0, 220]);
  const y = d3.scaleLog([Math.max(1, d3.min(vals, (v) => v[1])), d3.max(vals, (v) => v[1]) * 1.05], [60, 4]).clamp(true);
  const line = d3.line().x((v) => x(v[0])).y((v) => y(Math.max(1, v[1])))(vals);
  const q = gaugeValue(d, 'q', idx);
  const wt = gaugeValue(d, 'wt', idx);
  tip.show(`<div style="font-weight:600;margin-bottom:2px">${d.name}</div>
    <div style="color:var(--muted)">USGS ${d.id} · ${d3.format(',')(d.drainageSqMi)} sq mi</div>
    <div style="margin:4px 0" class="num">${fmtCfs(q)} ${wt != null ? ` · ${fmtF(wt)}` : ''} <span style="color:var(--muted)">(${(q / d.meanQ).toFixed(1)}× window mean)</span></div>
    <svg width="220" height="64"><path d="${line}" fill="none" stroke="#80ffdb" stroke-width="1.4"/><line x1="${x(idx)}" x2="${x(idx)}" y1="0" y2="64" stroke="#ffb703"/></svg>`, event);
  drawTimeline();
}

// ---------- particles ----------
const P = {
  n: 0,
  seg: new Int32Array(MAX_PARTICLES),
  pos: new Float32Array(MAX_PARTICLES),
  age: new Float32Array(MAX_PARTICLES),
};

let colorMode = 'flow';
const flowColor = d3.scaleSequentialLog([0.4, 25], (t) => d3.interpolateRgbBasis(['#3a0ca3', '#4361ee', '#4cc9f0', '#80ffdb', '#fff3b0', '#ffb703', '#fb5607'])(t)).clamp(true);
const tempColor = d3.scaleSequential([45, 82], (t) => d3.interpolateRgbBasis(['#4361ee', '#4cc9f0', '#80ffdb', '#ffd166', '#fb5607', '#d00000'])(t)).clamp(true);
const BUCKETS = 24;
const bucketColors = { flow: [], temp: [] };
for (let b = 0; b < BUCKETS; b++) {
  bucketColors.flow.push(flowColor(Math.exp(Math.log(0.4) + (b / (BUCKETS - 1)) * (Math.log(25) - Math.log(0.4)))));
  bucketColors.temp.push(tempColor(45 + (b / (BUCKETS - 1)) * 37));
}
function drawLegend() {
  const c = document.getElementById('legend').getContext('2d');
  bucketColors[colorMode].forEach((col, b) => { c.fillStyle = col; c.fillRect((b / BUCKETS) * 140, 0, 140 / BUCKETS + 1, 8); });
  document.getElementById('lg-lo').textContent = colorMode === 'flow' ? '0.4× normal' : '45 °F';
  document.getElementById('lg-hi').textContent = colorMode === 'flow' ? '25×' : '82 °F';
}

function bucketOf(si) {
  if (colorMode === 'flow') {
    const r = state.flow[si] / segs[si].meanFlowCfs;
    return clamp(Math.round(((Math.log(r) - Math.log(0.4)) / (Math.log(25) - Math.log(0.4))) * (BUCKETS - 1)), 0, BUCKETS - 1);
  }
  const t = state.wt[si];
  if (Number.isNaN(t)) return -1;
  return clamp(Math.round(((t - 45) / 37) * (BUCKETS - 1)), 0, BUCKETS - 1);
}

// Emission ∝ local runoff (reach flow minus inflow from upstream reaches).
const local = new Float32Array(segs.length);
function emit(dtSec) {
  const { flow } = state;
  let total = 0;
  for (const s of segs) {
    let up = 0;
    for (const u of s.ups) up += flow[u.i];
    local[s.i] = Math.max(0, flow[s.i] - up) + (s.ups.length ? 0 : flow[s.i] * 0.02);
    total += local[s.i];
  }
  const outletQ = state.outlet ? flow[state.outlet.seg.i] : total;
  // Scale so particle count tracks outlet discharge sub-linearly and stays within budget.
  const perCfs = (3.2 * Math.pow(outletQ, 0.72)) / Math.max(total, 1);
  for (const s of segs) {
    const expected = local[s.i] * perCfs * dtSec;
    let k = Math.floor(expected) + (Math.random() < expected % 1 ? 1 : 0);
    while (k-- > 0 && P.n < MAX_PARTICLES) {
      P.seg[P.n] = s.i;
      P.pos[P.n] = Math.random() * geo.len[s.i];
      P.age[P.n] = 0;
      P.n++;
    }
  }
}

function step(dtSec) {
  const { flow } = state;
  const scale = size.w / 1400;
  for (let p = 0; p < P.n; p++) {
    const si = P.seg[p];
    const s = segs[si];
    const ratio = flow[si] / s.meanFlowCfs;
    const v = (18 + 14 * (s.velocityFps || 1)) * Math.pow(Math.max(ratio, 0.05), 0.35) * scale;
    P.pos[p] += v * dtSec;
    P.age[p] += dtSec;
    if (P.pos[p] >= geo.len[si]) {
      const next = s.down;
      if (!next) { kill(p); p--; continue; }
      P.pos[p] -= geo.len[si];
      P.seg[p] = next.i;
    }
  }
}
function kill(p) {
  const last = --P.n;
  P.seg[p] = P.seg[last];
  P.pos[p] = P.pos[last];
  P.age[p] = P.age[last];
}

function pointOn(si, d) {
  const pts = geo.pts[si];
  const cum = geo.cum[si];
  let lo = 0;
  let hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid; else hi = mid;
  }
  const seg = cum[hi] - cum[lo] || 1;
  const t = clamp((d - cum[lo]) / seg, 0, 1);
  return [pts[lo][0] + (pts[hi][0] - pts[lo][0]) * t, pts[lo][1] + (pts[hi][1] - pts[lo][1]) * t];
}

const bucketLists = Array.from({ length: BUCKETS + 1 }, () => []);
function drawParticles() {
  const { w, h } = size;
  pctx.globalCompositeOperation = 'destination-out';
  pctx.fillStyle = 'rgba(0,0,0,0.16)';
  pctx.fillRect(0, 0, w, h);
  pctx.globalCompositeOperation = 'source-over';
  pctx.globalAlpha = 0.9;
  const segBucket = new Int16Array(segs.length);
  for (const s of segs) segBucket[s.i] = bucketOf(s.i);
  bucketLists.forEach((l) => (l.length = 0));
  for (let p = 0; p < P.n; p++) bucketLists[segBucket[P.seg[p]] + 1].push(p);
  const r = size.w > 1600 ? 1.9 : 1.5;
  bucketLists.forEach((list, b) => {
    if (!list.length) return;
    pctx.fillStyle = b === 0 ? 'rgba(160,170,190,0.6)' : bucketColors[colorMode][b - 1];
    pctx.beginPath();
    for (const p of list) {
      const [x, y] = pointOn(P.seg[p], P.pos[p]);
      pctx.rect(x - r / 2, y - r / 2, r, r);
    }
    pctx.fill();
  });
  pctx.globalAlpha = 1;
}

// ---------- timeline ----------
const tl = d3.select('#timeline svg');
function drawTimeline() {
  const node = tl.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 44, r: 10, t: 8, b: 18 };
  const { t0, n, outlet, wx0 } = state;
  const x = d3.scaleUtc([new Date(t0), new Date(t0 + (n - 1) * HOUR)], [m.l, w - m.r]);
  const qs = outlet.q;
  const y = d3.scaleLinear([0, d3.max(qs) * 1.08], [h - m.b, m.t]).nice();
  const precip = d3.range(n).map((i) => {
    const wi = Math.round((t0 + i * HOUR - wx0) / HOUR);
    return weather.precipitation[wi] ?? null;
  });
  const yp = d3.scaleLinear([0, Math.max(0.15, d3.max(precip) ?? 0.15)], [m.t, (h - m.b) * 0.55]);
  tl.selectAll('*').remove();
  tl.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(w / 110).tickSizeOuter(0));
  tl.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3).tickFormat(d3.format('~s')));
  const bw = Math.max(1, (w - m.l - m.r) / n);
  tl.append('g').selectAll('rect').data(precip.map((v, i) => [i, v]).filter(([, v]) => v > 0.002)).join('rect')
    .attr('x', ([i]) => x(new Date(t0 + i * HOUR)))
    .attr('y', m.t)
    .attr('width', bw)
    .attr('height', ([, v]) => yp(v) - m.t)
    .attr('fill', 'rgba(123,140,255,0.7)');
  const area = d3.area().defined((v) => v != null).x((_, i) => x(new Date(t0 + i * HOUR))).y0(h - m.b).y1((v) => y(v));
  const grad = tl.append('defs').append('linearGradient').attr('id', 'qg').attr('x1', 0).attr('x2', 0).attr('y1', 0).attr('y2', 1);
  grad.append('stop').attr('offset', '0%').attr('stop-color', '#4cc9f0').attr('stop-opacity', 0.6);
  grad.append('stop').attr('offset', '100%').attr('stop-color', '#4cc9f0').attr('stop-opacity', 0.02);
  tl.append('path').attr('d', area(qs)).attr('fill', 'url(#qg)');
  tl.append('path').attr('d', area.lineY1()(qs)).attr('fill', 'none').attr('stroke', '#4cc9f0').attr('stroke-width', 1.3);
  if (hovered && hovered !== outlet) {
    const yh = d3.scaleLinear([0, d3.max(hovered.q) * 1.08], [h - m.b, m.t]);
    tl.append('path').attr('d', d3.line().defined((v) => v != null).x((_, i) => x(new Date(t0 + i * HOUR))).y((v) => yh(v))(hovered.q)).attr('fill', 'none').attr('stroke', '#80ffdb').attr('stroke-dasharray', '3 2');
    tl.append('text').attr('x', w - m.r).attr('y', m.t + 10).attr('text-anchor', 'end').attr('fill', '#80ffdb').style('fill', '#80ffdb').style('font-size', '11px').text(`${hovered.name} (own scale)`);
  }
  tl.append('text').attr('x', m.l + 6).attr('y', h - m.b - 6).style('font-size', '11px').text('Delaware at Callicoon (cfs)');
  tl.append('text').attr('x', m.l + 6).attr('y', m.t + 11).style('font-size', '11px').style('fill', '#9aa6ff').text('basin rain (hourly)');
  tl.append('line').attr('class', 'cursor').attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.5);
  tl.append('circle').attr('class', 'cursor-dot').attr('r', 4).attr('fill', '#ffb703');
  tl.on('pointerdown pointermove', (event) => {
    if (event.type === 'pointermove' && event.buttons !== 1) return;
    const t = x.invert(d3.pointer(event)[0]).getTime();
    clock = clamp(t, t0, t0 + (n - 1) * HOUR);
  });
  tl.node().__x = x;
  tl.node().__y = y;
}
function updateCursor() {
  const x = tl.node().__x;
  const y = tl.node().__y;
  const cx = x(new Date(clock));
  tl.select('.cursor').attr('x1', cx).attr('x2', cx);
  const idx = (clock - state.t0) / HOUR;
  tl.select('.cursor-dot').attr('cx', cx).attr('cy', y(gaugeValue(state.outlet, 'q', idx) ?? 0));
}

// ---------- loop ----------
let clock = 0;
let playing = true;
let speed = 8;
let last = performance.now();
let uiTick = 0;

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const { t0, n } = state;
  if (playing) {
    clock += dt * speed * HOUR;
    if (clock > t0 + (n - 1) * HOUR) clock = t0;
  }
  const idx = (clock - t0) / HOUR;
  updateFlows(idx);
  emit(dt);
  step(dt);
  drawParticles();
  if ((uiTick += dt) > 0.08) {
    uiTick = 0;
    updateUI(idx);
  }
  requestAnimationFrame(frame);
}

const qScale = d3.scaleSqrt([0, 25000], [2.5, 26]).clamp(true);
function updateUI(idx) {
  document.getElementById('clock').textContent = fmtET(new Date(clock));
  document.getElementById('qout').textContent = fmtCfs(gaugeValue(state.outlet, 'q', idx));
  const wi = Math.round((clock - state.wx0) / HOUR);
  const rain = weather.precipitation.slice(Math.max(0, wi - 24), wi + 1);
  document.getElementById('rain24').textContent = rain.length && wi < weather.length ? `${d3.sum(rain).toFixed(2)} in` : 'n/a';
  document.getElementById('pcount').textContent = `${d3.format(',')(P.n)} water parcels in motion`;
  overlay.selectAll('g.gauge').each(function (d) {
    const q = gaugeValue(d, 'q', idx) ?? 0;
    const sel = d3.select(this);
    sel.select('.dot').attr('r', d.id === '01427510' ? 6 : 3.5);
    sel.select('.halo').attr('r', qScale(q)).attr('stroke-opacity', clamp(0.15 + 0.5 * (q / d.meanQ - 1), 0.12, 0.8));
  });
  updateCursor();
}

// ---------- controls ----------
const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('speed').onchange = (e) => { speed = +e.target.value; };
document.getElementById('window').onchange = async (e) => {
  const key = e.target.value;
  gaugeSets[key] ??= await loadJSON(`gauges-${key}.json`);
  setWindow(gaugeSets[key]);
};
for (const mode of ['flow', 'temp']) {
  document.getElementById(`c-${mode}`).onclick = () => {
    colorMode = mode;
    document.getElementById('c-flow').classList.toggle('active', mode === 'flow');
    document.getElementById('c-temp').classList.toggle('active', mode === 'temp');
    drawLegend();
  };
}
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });

function setWindow(set) {
  state = prepareWindow(set);
  P.n = 0;
  const qs = state.outlet.q;
  // Start a little before the peak so the flood arrives soon after load.
  const peak = qs.indexOf(d3.max(qs));
  clock = state.t0 + Math.max(0, set === gaugeSets.storm ? peak - 40 : 0) * HOUR;
  if (size) { drawOverlayStatic(); drawTimeline(); }
}

setWindow(gaugeSets.storm);
const relayout = () => projection && requestAnimationFrame(layout);
size = hiDpiCanvas(baseCanvas, relayout);
hiDpiCanvas(pCanvas, relayout);
layout();
drawTimeline();
drawLegend();
new ResizeObserver(() => drawTimeline()).observe(tl.node());
requestAnimationFrame(frame);
