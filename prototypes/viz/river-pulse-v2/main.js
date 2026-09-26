import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, tooltip, fmtCfs, fmtF, fmtET, hiDpiCanvas, HOUR, clamp } from '../shared/common.js';
import { buildNetwork, snapGauge, SQMI_TO_SQKM } from '../shared/network.js';
import { loadHourlyGrid, loadTerrain } from '../shared/hourly-grid.js';
import { createWeatherLayer, RAIN_LEGEND } from './weather-layer.js';

mountTopbar('River Pulse v2', 'Rain, sun & snowmelt feeding the network · upper Delaware');
mountSource('Flow: USGS NWIS IV (27 gauges, provisional). Weather: Open-Meteo historical API, ECMWF IFS 9 km, hourly, 0.1° grid (interpolated). Terrain: AWS Terrain Tiles. Network: NHDPlus V2; basin: USGS NLDI.');

const MAX_PARTICLES = 30000;
const MAX_HILL = 6000;
const HILLSLOPE_MS = 0.1; // conceptual hillslope travel speed (m/s)
const PARCEL_MM_PX = 520; // water volume one tracer represents (mm × offscreen px)
const BASE_EMIT = 0.6;

const params = new URLSearchParams(location.search);
const [basin, rivers, waterbodies, terrain] = await Promise.all([
  loadJSON('basin.json'),
  loadJSON('rivers.json'),
  loadJSON('waterbodies.json'),
  loadTerrain(),
]);
const cache = { gauges: {}, grid: {} };
async function loadWindow(key) {
  cache.gauges[key] ??= await loadJSON(`gauges-${key}.json`);
  cache.grid[key] ??= await loadHourlyGrid(key);
  return { set: cache.gauges[key], grid: cache.grid[key] };
}

const tip = tooltip();
const baseCanvas = document.getElementById('base');
const wxCanvas = document.getElementById('weather');
const pCanvas = document.getElementById('particles');
const fxCanvas = document.getElementById('fx');
const overlay = d3.select('#overlay');
const pctx = pCanvas.getContext('2d');
const fctx = fxCanvas.getContext('2d');
const wxLayer = createWeatherLayer(wxCanvas, terrain, basin);
const layers = { rain: true, tracers: true, sun: true, snow: true };

// ---------- network ----------
const segs = buildNetwork(rivers);
const outletSeg = segs.find((s) => s.comid === 2617456);
let state = null;

function prepareWindow({ set, grid }, key) {
  const gauges = set.sites
    .filter((g) => g.drainageSqMi)
    .map((g) => ({ ...g, areaKm: g.drainageSqMi * SQMI_TO_SQKM }));
  for (const g of gauges) {
    g.seg = snapGauge(segs, g);
    g.meanQ = d3.mean(g.q.filter((v) => v != null));
  }
  const gaugeAt = new Map();
  for (const g of gauges) if (g.seg && (!gaugeAt.has(g.seg) || gaugeAt.get(g.seg).areaKm < g.areaKm)) gaugeAt.set(g.seg, g);
  for (const s of segs) {
    s.chain = [];
    for (let cur = s; cur && s.chain.length < 4; cur = cur.down) {
      const g = gaugeAt.get(cur);
      if (g) s.chain.push(g);
    }
  }
  const t0 = Date.parse(set.start);
  const series = { precip: grid.basinMean('precip'), snow: grid.basinMean('snowDepth'), sw: grid.basinMean('sw') };
  return {
    key, set, grid, gauges, series, t0, n: set.length,
    outlet: gauges.find((g) => g.id === '01427510'),
    flow: new Float32Array(segs.length),
    wt: new Float32Array(segs.length),
    arrivals: { rain: new Float32Array(set.length), melt: new Float32Array(set.length) },
  };
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

// ---------- projection, static layers, spatial index ----------
let projection;
let size;
let metersPerPx = 100;
const geo = { pts: [], cum: [], len: new Float32Array(segs.length) };
const HASH = 14;
let hash = new Map();

function layout() {
  const { w, h } = size;
  const right = w > 900 ? 310 : 20;
  projection = d3.geoMercator().fitExtent([[40, 70], [w - right - 20, h - 180]], basin.features[0]);
  const c = [w / 2, h / 2];
  metersPerPx = (d3.geoDistance(projection.invert(c), projection.invert([c[0] + 100, c[1]])) * 6371000) / 100;
  hash = new Map();
  segs.forEach((s) => {
    const pts = s.coords.map((cc) => projection(cc));
    const cum = [0];
    for (let k = 1; k < pts.length; k++) cum.push(cum[k - 1] + Math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]));
    geo.pts[s.i] = pts;
    geo.cum[s.i] = cum;
    geo.len[s.i] = cum[cum.length - 1] || 0.01;
    pts.forEach(([x, y], k) => {
      const key = `${Math.floor(x / HASH)},${Math.floor(y / HASH)}`;
      if (!hash.has(key)) hash.set(key, []);
      hash.get(key).push([s.i, cum[k], x, y]);
    });
  });
  wxLayer.layout(projection);
  drawBase();
  drawOverlayStatic();
  pctx.clearRect(0, 0, w, h);
}

function nearestChannel(x, y) {
  const cx = Math.floor(x / HASH);
  const cy = Math.floor(y / HASH);
  for (let r = 0; r <= 5; r++) {
    let best = null;
    let bestD = Infinity;
    for (let i = cx - r; i <= cx + r; i++) {
      for (let j = cy - r; j <= cy + r; j++) {
        if (Math.max(Math.abs(i - cx), Math.abs(j - cy)) !== r) continue;
        for (const v of hash.get(`${i},${j}`) ?? []) {
          const d = Math.hypot(v[2] - x, v[3] - y);
          if (d < bestD) { bestD = d; best = v; }
        }
      }
    }
    if (best && bestD <= (r + 1) * HASH) return { seg: best[0], pos: best[1], x: best[2], y: best[3], d: bestD };
  }
  return null;
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
  const byName = d3.group(segs.filter((s) => s.name && s.order >= 3), (s) => s.name);
  const labels = [];
  for (const [name, list] of byName) {
    if (d3.sum(list, (s) => s.lengthKm) < 18) continue;
    const mid = list.sort((a, b) => a.hydroseq - b.hydroseq)[Math.floor(list.length / 2)];
    const pts = geo.pts[mid.i];
    let ang = (Math.atan2(pts.at(-1)[1] - pts[0][1], pts.at(-1)[0] - pts[0][0]) * 180) / Math.PI;
    if (ang > 90) ang -= 180;
    if (ang < -90) ang += 180;
    const p = pts[Math.floor(pts.length / 2)];
    labels.push({ name, x: p[0], y: p[1], ang });
  }
  overlay.append('g').selectAll('text').data(labels).join('text')
    .attr('class', 'river-label').attr('transform', (d) => `translate(${d.x},${d.y - 6}) rotate(${d.ang})`).attr('text-anchor', 'middle').text((d) => d.name);
  overlay.append('g').selectAll('text').data(waterbodies.features.filter((f) => f.properties.name && f.properties.areaSqKm > 5)).join('text')
    .attr('class', 'res-label').attr('x', (d) => path.centroid(d)[0]).attr('y', (d) => path.centroid(d)[1] + 18).attr('text-anchor', 'middle').text((d) => d.properties.name);
  const g = overlay.append('g').selectAll('g.gauge').data(state.gauges.filter((d) => d.seg), (d) => d.id).join('g').attr('class', 'gauge')
    .attr('transform', (d) => `translate(${projection([d.lon, d.lat])})`)
    .on('mousemove', (event, d) => showGauge(event, d))
    .on('mouseleave', () => tip.hide());
  g.append('circle').attr('class', 'halo').attr('fill', 'none').attr('stroke', '#80ffdb').attr('stroke-opacity', 0.5);
  g.append('circle').attr('class', 'dot').attr('r', (d) => (d.id === '01427510' ? 6 : 3.5)).attr('fill', (d) => (d.id === '01427510' ? '#80ffdb' : '#0b1222')).attr('stroke', '#80ffdb').attr('stroke-width', 1.5);
}

function showGauge(event, d) {
  const idx = (clock - state.t0) / HOUR;
  const q = gaugeValue(d, 'q', idx);
  const wt = gaugeValue(d, 'wt', idx);
  tip.show(`<div style="font-weight:600">${d.name}</div><div style="color:var(--muted)">USGS ${d.id} · ${d3.format(',')(d.drainageSqMi)} sq mi</div><div class="num">${fmtCfs(q)}${wt != null ? ` · ${fmtF(wt)}` : ''}</div>`, event);
}

// ---------- channel particles (0 = base flow, 1 = rain tracer, 2 = melt tracer) ----------
const P = { n: 0, seg: new Int32Array(MAX_PARTICLES), pos: new Float32Array(MAX_PARTICLES), kind: new Uint8Array(MAX_PARTICLES) };
function addParticle(seg, pos, kind) {
  if (P.n >= MAX_PARTICLES) return;
  P.seg[P.n] = seg;
  P.pos[P.n] = pos;
  P.kind[P.n] = kind;
  P.n++;
}
function kill(p) {
  const last = --P.n;
  P.seg[p] = P.seg[last];
  P.pos[p] = P.pos[last];
  P.kind[p] = P.kind[last];
}

// Hillslope parcels travelling from where rain/melt landed to the nearest stream.
const Hs = [];

let colorMode = 'flow';
const flowColor = d3.scaleSequentialLog([0.4, 25], (t) => d3.interpolateRgbBasis(['#3a0ca3', '#4361ee', '#4cc9f0', '#80ffdb', '#fff3b0', '#ffb703', '#fb5607'])(t)).clamp(true);
const tempColor = d3.scaleSequential([35, 82], (t) => d3.interpolateRgbBasis(['#4361ee', '#4cc9f0', '#80ffdb', '#ffd166', '#fb5607', '#d00000'])(t)).clamp(true);
const BUCKETS = 16;
const bucketColors = { flow: [], temp: [] };
for (let b = 0; b < BUCKETS; b++) {
  bucketColors.flow.push(flowColor(Math.exp(Math.log(0.4) + (b / (BUCKETS - 1)) * (Math.log(25) - Math.log(0.4)))));
  bucketColors.temp.push(tempColor(35 + (b / (BUCKETS - 1)) * 47));
}
function bucketOf(si) {
  if (colorMode === 'flow') {
    const r = state.flow[si] / segs[si].meanFlowCfs;
    return clamp(Math.round(((Math.log(r) - Math.log(0.4)) / (Math.log(25) - Math.log(0.4))) * (BUCKETS - 1)), 0, BUCKETS - 1);
  }
  const t = state.wt[si];
  return Number.isNaN(t) ? -1 : clamp(Math.round(((t - 35) / 47) * (BUCKETS - 1)), 0, BUCKETS - 1);
}

const local = new Float32Array(segs.length);
function emitBase(dtH) {
  const { flow } = state;
  let total = 0;
  for (const s of segs) {
    let up = 0;
    for (const u of s.ups) up += flow[u.i];
    local[s.i] = Math.max(0, flow[s.i] - up) + (s.ups.length ? 0 : flow[s.i] * 0.02);
    total += local[s.i];
  }
  const perCfs = (BASE_EMIT * Math.pow(flow[outletSeg.i], 0.72)) / Math.max(total, 1);
  for (const s of segs) {
    const expected = local[s.i] * perCfs * dtH;
    let k = Math.floor(expected) + (Math.random() < expected % 1 ? 1 : 0);
    while (k-- > 0) addParticle(s.i, Math.random() * geo.len[s.i], 0);
  }
}

/** Advance channel particles by dtH simulated hours at physical velocity. */
function stepChannel(dtH, hourIdx) {
  const { flow } = state;
  const pxPerMeter = 1 / metersPerPx;
  for (let p = 0; p < P.n; p++) {
    const si = P.seg[p];
    const s = segs[si];
    const ratio = flow[si] / s.meanFlowCfs;
    const vms = clamp((s.velocityFps || 1) * 0.3048 * Math.pow(Math.max(ratio, 0.05), 0.4), 0.15, 4);
    P.pos[p] += vms * 3600 * dtH * pxPerMeter;
    let exited = false;
    while (P.pos[p] >= geo.len[P.seg[p]]) {
      const cur = segs[P.seg[p]];
      if (!cur.down) {
        if (cur === outletSeg && P.kind[p] && hourIdx >= 0 && hourIdx < state.n) state.arrivals[P.kind[p] === 1 ? 'rain' : 'melt'][hourIdx] += 1;
        exited = true;
        break;
      }
      P.pos[p] -= geo.len[P.seg[p]];
      P.seg[p] = cur.down.i;
    }
    if (exited) { kill(p); p--; }
  }
}

function stepHillslope(dtH) {
  for (let k = Hs.length - 1; k >= 0; k--) {
    const h = Hs[k];
    h.t += dtH;
    if (h.t >= h.T) {
      addParticle(h.seg, h.pos, h.kind);
      Hs[k] = Hs[Hs.length - 1];
      Hs.pop();
    }
  }
}

// Visual rain streaks / snowflakes / melt sparkles (real-time), which hand off to hillslope parcels.
const fx = [];
function landParcel(x, y, kind) {
  const ch = nearestChannel(x, y);
  if (!ch || Hs.length >= MAX_HILL) return;
  const meters = ch.d * metersPerPx;
  const T = Math.max(0.3, (meters / HILLSLOPE_MS / 3600) * (0.5 + Math.random()));
  Hs.push({ x0: x, y0: y, x1: ch.x, y1: ch.y, seg: ch.seg, pos: ch.pos, t: 0, T, kind });
}

let spawnAcc = { rain: 0, snow: 0, melt: 0 };
function spawnWeather(dtH) {
  if (!layers.tracers) return;
  const tot = wxLayer.totals;
  spawnAcc.rain += (tot.rain * dtH) / PARCEL_MM_PX;
  spawnAcc.melt += (tot.melt * dtH) / PARCEL_MM_PX;
  spawnAcc.snow += (tot.snow * dtH) / (PARCEL_MM_PX * 0.5);
  while (spawnAcc.rain >= 1) {
    spawnAcc.rain -= 1;
    const pt = wxLayer.sample('rain');
    if (pt) fx.push({ type: 'rain', x: pt[0], y: pt[1], life: 0, dur: 0.38 });
  }
  while (spawnAcc.melt >= 1) {
    spawnAcc.melt -= 1;
    const pt = wxLayer.sample('melt');
    if (pt) fx.push({ type: 'melt', x: pt[0], y: pt[1], life: 0, dur: 0.5 });
  }
  while (spawnAcc.snow >= 1) {
    spawnAcc.snow -= 1;
    const pt = wxLayer.sample('snow');
    if (pt) fx.push({ type: 'snow', x: pt[0], y: pt[1], life: 0, dur: 1.4, ph: Math.random() * 6 });
  }
  if (fx.length > 2500) fx.splice(0, fx.length - 2500);
}

function drawFx(dt) {
  const { w, h } = size;
  fctx.clearRect(0, 0, w, h);
  fctx.lineCap = 'round';
  const rain = new Path2D();
  const splash = new Path2D();
  const snow = new Path2D();
  const melt = new Path2D();
  for (let k = fx.length - 1; k >= 0; k--) {
    const f = fx[k];
    f.life += dt;
    const t = f.life / f.dur;
    if (t >= 1) {
      if (f.type !== 'snow') landParcel(f.x, f.y, f.type === 'rain' ? 1 : 2);
      fx[k] = fx[fx.length - 1];
      fx.pop();
      continue;
    }
    if (f.type === 'rain') {
      if (t < 0.7) {
        const y = f.y - 26 * (1 - t / 0.7);
        rain.moveTo(f.x + 2 * (1 - t / 0.7), y - 7);
        rain.lineTo(f.x, y);
      } else {
        const r = 1 + ((t - 0.7) / 0.3) * 4;
        splash.moveTo(f.x + r, f.y);
        splash.arc(f.x, f.y, r, 0, Math.PI * 2);
      }
    } else if (f.type === 'snow') {
      const y = f.y - 20 * (1 - t);
      snow.moveTo(f.x + Math.sin(f.ph + t * 6) * 2 + 1.3, y);
      snow.arc(f.x + Math.sin(f.ph + t * 6) * 2, y, 1.3, 0, Math.PI * 2);
    } else {
      const r = 1.5 + Math.sin(t * Math.PI) * 2;
      melt.moveTo(f.x + r, f.y);
      melt.arc(f.x, f.y, r, 0, Math.PI * 2);
    }
  }
  fctx.strokeStyle = 'rgba(200,220,255,0.75)';
  fctx.lineWidth = 1;
  fctx.stroke(rain);
  fctx.strokeStyle = 'rgba(189,224,254,0.6)';
  fctx.stroke(splash);
  fctx.fillStyle = 'rgba(255,255,255,0.85)';
  fctx.fill(snow);
  fctx.fillStyle = 'rgba(215,184,255,0.8)';
  fctx.fill(melt);
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
  const t = clamp((d - cum[lo]) / (cum[hi] - cum[lo] || 1), 0, 1);
  return [pts[lo][0] + (pts[hi][0] - pts[lo][0]) * t, pts[lo][1] + (pts[hi][1] - pts[lo][1]) * t];
}

const bucketLists = Array.from({ length: BUCKETS + 1 }, () => []);
function drawParticles() {
  const { w, h } = size;
  pctx.globalCompositeOperation = 'destination-out';
  pctx.fillStyle = 'rgba(0,0,0,0.18)';
  pctx.fillRect(0, 0, w, h);
  pctx.globalCompositeOperation = 'source-over';
  const segBucket = new Int16Array(segs.length);
  for (const s of segs) segBucket[s.i] = bucketOf(s.i);
  bucketLists.forEach((l) => (l.length = 0));
  const tracers = { 1: new Path2D(), 2: new Path2D() };
  const r = size.w > 1600 ? 1.7 : 1.4;
  const rt = r * 1.7;
  for (let p = 0; p < P.n; p++) {
    if (P.kind[p]) {
      if (!layers.tracers) continue;
      const [x, y] = pointOn(P.seg[p], P.pos[p]);
      tracers[P.kind[p]].rect(x - rt / 2, y - rt / 2, rt, rt);
    } else bucketLists[segBucket[P.seg[p]] + 1].push(p);
  }
  pctx.globalAlpha = layers.tracers ? 0.5 : 0.9;
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
  if (layers.tracers) {
    pctx.fillStyle = '#d8ecff';
    pctx.fill(tracers[1]);
    pctx.fillStyle = '#d7b8ff';
    pctx.fill(tracers[2]);
    const hill = { 1: new Path2D(), 2: new Path2D() };
    for (const hp of Hs) {
      const t = hp.t / hp.T;
      const x = hp.x0 + (hp.x1 - hp.x0) * t;
      const y = hp.y0 + (hp.y1 - hp.y0) * t;
      hill[hp.kind].rect(x - 0.8, y - 0.8, 1.6, 1.6);
    }
    pctx.fillStyle = 'rgba(160,200,255,0.85)';
    pctx.fill(hill[1]);
    pctx.fillStyle = 'rgba(210,180,255,0.85)';
    pctx.fill(hill[2]);
  }
}

// ---------- timeline ----------
const tl = d3.select('#timeline svg');
let tlScales = null;
function drawTimeline() {
  const node = tl.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 44, r: 50, t: 8, b: 18 };
  const { t0, n, outlet, series, grid } = state;
  const x = d3.scaleUtc([new Date(t0), new Date(t0 + (n - 1) * HOUR)], [m.l, w - m.r]);
  const qs = outlet.q;
  const y = d3.scaleLinear([0, d3.max(qs) * 1.08], [h - m.b, m.t]).nice();
  const gOff = Math.round((t0 - grid.t0) / HOUR);
  const at = (arr, i) => arr[i + gOff];
  const yp = d3.scaleLinear([0, Math.max(1, d3.max(series.precip))], [m.t, (h - m.b) * 0.5]);
  const ys = d3.scaleLinear([0, Math.max(0.1, d3.max(series.snow))], [h - m.b, m.t + 30]);
  const ysw = d3.scaleLinear([0, 900], [h - m.b, m.t + 10]);
  tl.selectAll('*').remove();
  tl.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(w / 110).tickSizeOuter(0));
  tl.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3).tickFormat(d3.format('~s')));
  const xi = (i) => x(new Date(t0 + i * HOUR));
  tl.append('path').attr('d', d3.area().defined((_, i) => at(series.snow, i) != null).x((_, i) => xi(i)).y0(h - m.b).y1((_, i) => ys(at(series.snow, i) ?? 0))(d3.range(n))).attr('fill', 'rgba(238,242,255,0.14)').attr('stroke', 'rgba(238,242,255,0.4)');
  tl.append('path').attr('d', d3.line().x((i) => xi(i)).y((i) => ysw(at(series.sw, i) ?? 0))(d3.range(n))).attr('fill', 'none').attr('stroke', 'rgba(255,176,64,0.45)').attr('stroke-width', 0.8);
  const bw = Math.max(1, (w - m.l - m.r) / n);
  tl.append('g').selectAll('rect').data(d3.range(n).filter((i) => at(series.precip, i) > 0.05)).join('rect')
    .attr('x', xi).attr('y', m.t).attr('width', bw).attr('height', (i) => yp(at(series.precip, i)) - m.t).attr('fill', 'rgba(123,140,255,0.75)');
  const area = d3.area().defined((v) => v != null).x((_, i) => xi(i)).y0(h - m.b).y1((v) => y(v));
  tl.append('path').attr('d', area(qs)).attr('fill', 'rgba(76,201,240,0.18)');
  tl.append('path').attr('d', area.lineY1()(qs)).attr('fill', 'none').attr('stroke', '#4cc9f0').attr('stroke-width', 1.3);
  tl.append('path').attr('class', 'arr-rain').attr('fill', 'none').attr('stroke', '#d8ecff').attr('stroke-width', 1.6);
  tl.append('path').attr('class', 'arr-melt').attr('fill', 'none').attr('stroke', '#d7b8ff').attr('stroke-width', 1.6);
  const leg = [['#4cc9f0', 'Callicoon flow (cfs)'], ['#9aa6ff', 'basin rain (bars, mm/h)'], ['#ffb040', 'shortwave'], ['#eef2ff', 'snow depth'], ['#d8ecff', 'tracer rain arriving at gauge'], ['#d7b8ff', 'tracer melt arriving']];
  const lg = tl.append('g').attr('transform', `translate(${m.l + 8},${h - m.b - 8})`);
  let lx = 0;
  leg.forEach(([c, t]) => {
    lg.append('rect').attr('x', lx).attr('y', -7).attr('width', 8).attr('height', 8).attr('fill', c);
    const tx = lg.append('text').attr('x', lx + 11).attr('y', 0).style('font-size', '10.5px').text(t);
    lx += 11 + tx.node().getComputedTextLength() + 14;
  });
  tl.append('line').attr('class', 'cursor').attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.5);
  tl.on('pointerdown pointermove', (event) => {
    if (event.type === 'pointermove' && event.buttons !== 1) return;
    clock = clamp(x.invert(d3.pointer(event)[0]).getTime(), t0, t0 + (n - 1) * HOUR);
    resetTransient();
  });
  tlScales = { x, xi, h, m };
}
function updateTimeline() {
  const { x, xi, h, m } = tlScales;
  const cx = x(new Date(clock));
  tl.select('.cursor').attr('x1', cx).attr('x2', cx);
  const smooth = (arr) => arr.map((_, i) => d3.mean(arr.slice(Math.max(0, i - 3), i + 4)));
  const r = smooth(Array.from(state.arrivals.rain));
  const mm = smooth(Array.from(state.arrivals.melt));
  const ya = d3.scaleLinear([0, Math.max(4, d3.max(r), d3.max(mm))], [h - m.b, m.t + 12]);
  const line = d3.line().defined((v) => v > 0).x((_, i) => xi(i)).y((v) => ya(v));
  tl.select('.arr-rain').attr('d', line(r));
  tl.select('.arr-melt').attr('d', line(mm));
}

// ---------- loop ----------
let clock = 0;
let playing = true;
let speed = 4;
let last = performance.now();
let uiTick = 0;
let lastFieldH = -1;

function resetTransient() {
  P.n = 0;
  Hs.length = 0;
  fx.length = 0;
  spawnAcc = { rain: 0, snow: 0, melt: 0 };
}

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const { t0, n } = state;
  const dtH = playing ? dt * speed : 0;
  if (playing) {
    clock += dtH * HOUR;
    if (clock > t0 + (n - 1) * HOUR) {
      clock = t0;
      resetTransient();
      state.arrivals.rain.fill(0);
      state.arrivals.melt.fill(0);
    }
  }
  const idx = (clock - t0) / HOUR;
  const gh = state.grid.hourAt(clock);
  if (Math.abs(gh - lastFieldH) > 0.02) {
    wxLayer.compute(gh, new Date(clock), layers);
    lastFieldH = gh;
  }
  const wctx = wxCanvas.getContext('2d');
  wctx.clearRect(0, 0, size.w, size.h);
  wxLayer.draw();
  updateFlows(idx);
  emitBase(dtH);
  spawnWeather(dtH);
  stepHillslope(dtH);
  stepChannel(dtH, Math.floor(idx));
  drawParticles();
  drawFx(playing ? dt : 0);
  if ((uiTick += dt) > 0.1) {
    uiTick = 0;
    updateUI(idx);
  }
  requestAnimationFrame(frame);
}

const qScale = d3.scaleSqrt([0, 25000], [2.5, 26]).clamp(true);
function updateUI(idx) {
  document.getElementById('clock').textContent = fmtET(new Date(clock));
  document.getElementById('qout').textContent = fmtCfs(gaugeValue(state.outlet, 'q', idx));
  const st = wxLayer.stats;
  const sun = wxLayer.sun;
  document.getElementById('s-rain').textContent = `${st.rain.toFixed(2)} mm/h`;
  document.getElementById('s-sun').textContent = sun.elevation > 0 ? `${sun.elevation.toFixed(0)}° up, az ${sun.azimuth.toFixed(0)}°` : 'below horizon';
  document.getElementById('s-sw').textContent = `${st.sw.toFixed(0)} W/m²`;
  document.getElementById('s-snow').textContent = `${(st.snow * 100).toFixed(1)} cm`;
  document.getElementById('s-melt').textContent = `${st.melt.toFixed(2)} mm/h`;
  const i = Math.floor(idx);
  const recent = d3.sum(state.arrivals.rain.slice(Math.max(0, i - 24), i + 1)) + d3.sum(state.arrivals.melt.slice(Math.max(0, i - 24), i + 1));
  document.getElementById('s-arr').textContent = `${recent} parcels/24 h`;
  overlay.selectAll('g.gauge').each(function (d) {
    const q = gaugeValue(d, 'q', idx) ?? 0;
    d3.select(this).select('.halo').attr('r', qScale(q)).attr('stroke-opacity', clamp(0.15 + 0.5 * (q / d.meanQ - 1), 0.12, 0.8));
  });
  updateTimeline();
}

function drawRainLegend() {
  const c = document.getElementById('legend-rain').getContext('2d');
  const sc = d3.scaleLog(RAIN_LEGEND.map((s) => s[0]), RAIN_LEGEND.map((s) => s[1])).clamp(true);
  for (let k = 0; k < 110; k++) { c.fillStyle = sc(Math.exp(Math.log(0.1) + (k / 109) * Math.log(250))); c.fillRect(k, 0, 1, 8); }
  document.getElementById('lg-rain').textContent = '0.1 → 25';
}

// ---------- controls ----------
const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('speed').onchange = (e) => { speed = +e.target.value; };
document.getElementById('window').onchange = async (e) => setWindow(e.target.value);
for (const key of Object.keys(layers)) {
  document.getElementById(`l-${key}`).onclick = (ev) => {
    layers[key] = !layers[key];
    ev.target.classList.toggle('active', layers[key]);
    lastFieldH = -1;
  };
}
for (const mode of ['flow', 'temp']) {
  document.getElementById(`c-${mode}`).onclick = () => {
    colorMode = mode;
    document.getElementById('c-flow').classList.toggle('active', mode === 'flow');
    document.getElementById('c-temp').classList.toggle('active', mode === 'temp');
  };
}
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });

/** Start shortly before the weather event of interest. */
function startClock() {
  const { grid, t0, n, key, series } = state;
  const gOff = Math.round((t0 - grid.t0) / HOUR);
  if (params.has('t')) return Date.parse(params.get('t'));
  if (key === 'melt') {
    let best = 24;
    for (let i = 24; i < n; i++) if ((series.snow[i - 24 + gOff] ?? 0) - (series.snow[i + gOff] ?? 0) > (series.snow[best - 24 + gOff] ?? 0) - (series.snow[best + gOff] ?? 0)) best = i;
    return t0 + Math.max(0, best - 60) * HOUR;
  }
  const peak = state.outlet.q.indexOf(d3.max(state.outlet.q));
  let i = Math.max(0, peak - 96);
  while (i < peak && (series.precip[i + gOff] ?? 0) < 0.8) i++;
  return t0 + Math.max(0, i - 14) * HOUR;
}

async function setWindow(key) {
  const data = await loadWindow(key);
  state = prepareWindow(data, key);
  wxLayer.setGrid(state.grid);
  lastFieldH = -1;
  resetTransient();
  clock = startClock();
  document.getElementById('window').value = key;
  if (size) { drawOverlayStatic(); drawTimeline(); }
}

await setWindow(params.get('window') === 'melt' ? 'melt' : 'storm');
if (params.has('paused')) playBtn.click();
if (params.has('speed')) { speed = +params.get('speed'); document.getElementById('speed').value = params.get('speed'); }
const relayout = () => projection && requestAnimationFrame(() => { layout(); drawTimeline(); });
size = hiDpiCanvas(baseCanvas, relayout);
hiDpiCanvas(wxCanvas, relayout);
hiDpiCanvas(pCanvas, relayout);
hiDpiCanvas(fxCanvas, relayout);
layout();
drawTimeline();
drawRainLegend();
requestAnimationFrame(frame);
