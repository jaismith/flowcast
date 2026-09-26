import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, tooltip, fmtCfs, fmtET, HOUR, clamp } from '../shared/common.js';
import { buildNetwork, snapGauge } from '../shared/network.js';

mountTopbar('Flood Wave', 'Joy-plot of every gauge · watch the wave travel downstream');
mountSource('USGS NWIS instantaneous discharge (hourly means, provisional) at 27 gauges upstream of Callicoon. River distance from NHDPlus V2 path length. Basin: USGS NLDI.');

const [basin, rivers] = await Promise.all([loadJSON('basin.json'), loadJSON('rivers.json')]);
const sets = { storm: await loadJSON('gauges-storm.json') };
const segs = buildNetwork(rivers);
const tip = tooltip();
const outletSeg = segs.find((s) => s.comid === 2617456);
// Gauges just below the Pepacton and Cannonsville dams report regulated releases.
const DAM_RELEASE = new Set(['01417000', '01425000']);

let state;
let order = 'dist';
let clock = 0;
let playing = true;

function prepare(set) {
  const t0 = Date.parse(set.start);
  // Peaks are taken within ±4 days of the outlet's peak so every gauge is timed on the same event.
  const outlet = set.sites.find((g) => g.id === '01427510');
  const oPeak = d3.greatest(outlet.q.map((v, i) => [i, v]).filter(([, v]) => v != null), (d) => d[1])[0];
  const gauges = set.sites
    .filter((g) => g.drainageSqMi && g.q.some((v) => v != null))
    .map((g) => {
      const seg = snapGauge(segs, g);
      const valid = g.q.map((v, i) => [i, v]).filter(([, v]) => v != null);
      const inEvent = valid.filter(([i]) => Math.abs(i - oPeak) <= 96);
      const [pi, pq] = d3.greatest(inEvent.length ? inEvent : valid, (d) => d[1]);
      const minQ = d3.min(valid, (d) => d[1]);
      const maxQ = d3.max(valid, (d) => d[1]);
      return { ...g, seg, distKm: seg ? seg.pathKm - outletSeg.pathKm : null, peakI: pi, peakQ: pq, maxQ, minQ };
    })
    .filter((g) => g.seg);
  return { set, t0, n: set.length, gauges, oPeak };
}

// ---------- ridges ----------
const svg = d3.select('#ridges svg');
const peakColor = d3.scaleSequential(d3.interpolateRgbBasis(['#7209b7', '#4361ee', '#4cc9f0', '#80ffdb', '#ffd166', '#fb5607']));
let geom;

function drawRidges() {
  const node = svg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 250, r: 20, t: 40, b: 26 };
  const { gauges, t0, n } = state;
  const rows = [...gauges].sort(order === 'dist' ? (a, b) => b.distKm - a.distKm : (a, b) => a.drainageSqMi - b.drainageSqMi);
  const x = d3.scaleUtc([new Date(t0), new Date(t0 + (n - 1) * HOUR)], [m.l, w - m.r]);
  const step = (h - m.t - m.b) / rows.length;
  const ridgeH = step * 3.2;
  const peakTimes = d3.extent(gauges, (g) => g.peakI);
  peakColor.domain([peakTimes[0] - 3, peakTimes[1] + 3]);
  svg.selectAll('*').remove();
  const defs = svg.append('defs');
  defs.append('clipPath').attr('id', 'past').append('rect').attr('x', 0).attr('y', 0).attr('height', h).attr('width', m.l);
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(w / 110).tickSizeOuter(0));
  svg.append('text').attr('x', m.l).attr('y', 14).style('font-size', '11px').text(state.set.label);
  const rowG = svg.append('g');
  const pastG = svg.append('g').attr('clip-path', 'url(#past)');
  const wave = [];
  rows.forEach((g, k) => {
    const y0 = m.t + (k + 1) * step;
    const yv = (v) => y0 - ((v - g.minQ) / (g.maxQ - g.minQ || 1)) * ridgeH;
    const area = d3.area().defined((v) => v != null).x((_, i) => x(new Date(t0 + i * HOUR))).y0(y0).y1((v) => yv(v)).curve(d3.curveMonotoneX);
    const line = area.lineY1();
    const col = peakColor(g.peakI);
    rowG.append('path').attr('d', area(g.q)).attr('fill', '#04070e');
    rowG.append('path').attr('d', line(g.q)).attr('fill', 'none').attr('stroke', 'rgba(255,255,255,0.13)').attr('stroke-width', 0.8);
    pastG.append('path').attr('d', area(g.q)).attr('fill', col).attr('fill-opacity', 0.28);
    pastG.append('path').attr('d', line(g.q)).attr('fill', 'none').attr('stroke', col).attr('stroke-width', 1.4);
    const px = x(new Date(t0 + g.peakI * HOUR));
    wave.push({ g, x: px, y: yv(g.peakQ), y0, col });
    const label = rowG.append('text').attr('class', 'row-label').attr('x', m.l - 8).attr('y', y0 - 2).attr('text-anchor', 'end')
      .text(`${g.name.replace(/ (NY|PA|NR|NEAR|AT|ABOVE)\b.*$/i, '').replace(/DELAWARE RIVER/i, 'Delaware R').replace(/WEST BRANCH/i, 'W Br').replace(/EAST BRANCH/i, 'E Br').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()).slice(0, 26)}${DAM_RELEASE.has(g.id) ? ' (dam release)' : ''} · ${order === 'dist' ? `${g.distKm.toFixed(0)} km` : `${g.drainageSqMi} mi²`}`);
    label.datum(g);
    rowG.append('rect').attr('x', 0).attr('width', w).attr('y', y0 - step).attr('height', step).attr('fill', 'transparent')
      .on('mousemove', (event) => {
        const i = clamp(Math.round((x.invert(d3.pointer(event)[0]) - t0) / HOUR), 0, n - 1);
        highlight(g);
        tip.show(`<b>${g.name}</b><br>USGS ${g.id} · ${g.drainageSqMi} sq mi · ${g.distKm.toFixed(0)} river-km above Callicoon<br>${fmtET(new Date(t0 + i * HOUR))}: <span class="num">${fmtCfs(g.q[i])}</span><br>Peak <span class="num">${fmtCfs(g.peakQ)}</span> at ${fmtET(new Date(t0 + g.peakI * HOUR))}`, event);
      })
      .on('mouseleave', () => { tip.hide(); highlight(null); });
  });
  const waveG = svg.append('g');
  const cursor = svg.append('line').attr('y1', m.t - 10).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.2);
  svg.on('pointerdown', (event) => {
    if (d3.pointer(event)[0] < m.l) return;
    clock = clamp(x.invert(d3.pointer(event)[0]).getTime(), t0, t0 + (n - 1) * HOUR);
  });
  geom = { x, m, h, w, wave, waveG, cursor };
}

function updateRidges() {
  const { x, m, wave, waveG, cursor } = geom;
  const cx = x(new Date(clock));
  d3.select('#past rect').attr('width', cx);
  cursor.attr('x1', cx).attr('x2', cx);
  const passed = wave.filter((d) => d.x <= cx);
  const dots = waveG.selectAll('g.peak').data(passed, (d) => d.g.id);
  const enter = dots.enter().append('g').attr('class', 'peak');
  enter.append('circle').attr('class', 'ripple').attr('cx', (d) => d.x).attr('cy', (d) => d.y).attr('r', 3).attr('fill', 'none').attr('stroke', (d) => d.col)
    .transition().duration(1200).ease(d3.easeCubicOut).attr('r', 22).attr('stroke-opacity', 0).remove();
  enter.append('circle').attr('cx', (d) => d.x).attr('cy', (d) => d.y).attr('r', 3.2).attr('fill', '#fff').attr('stroke', (d) => d.col).attr('stroke-width', 1.5);
  dots.exit().remove();
  svg.selectAll('.row-label').classed('lit', (g) => g && passed.some((p) => p.g === g));
  updateMap();
}

// ---------- mini map ----------
const mapSvg = d3.select('#mapPanel svg');
let mapGeom;
function drawMap() {
  const node = mapSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const proj = d3.geoMercator().fitExtent([[10, 10], [w - 10, h - 10]], basin);
  const path = d3.geoPath(proj);
  mapSvg.selectAll('*').remove();
  mapSvg.append('path').attr('d', path(basin)).attr('fill', '#0a1426').attr('stroke', 'rgba(128,255,219,0.35)').attr('stroke-dasharray', '3 3');
  mapSvg.append('g').selectAll('path').data(rivers.features.filter((f) => f.properties.order >= 3)).join('path')
    .attr('d', path).attr('fill', 'none').attr('stroke', 'rgba(76,150,220,0.5)').attr('stroke-width', (f) => (f.properties.order - 2) * 0.7);
  const dots = mapSvg.append('g').selectAll('g').data(state.gauges, (g) => g.id).join('g').attr('transform', (g) => `translate(${proj([g.lon, g.lat])})`);
  dots.append('circle').attr('class', 'halo').attr('r', 0).attr('fill', (g) => peakColor(g.peakI)).attr('fill-opacity', 0.35);
  dots.append('circle').attr('class', 'core').attr('r', 3).attr('fill', '#0b1222').attr('stroke', (g) => peakColor(g.peakI)).attr('stroke-width', 1.5);
  dots.on('mousemove', (event, g) => { highlight(g); tip.show(`<b>${g.name}</b><br>Peak ${fmtCfs(g.peakQ)} at ${fmtET(new Date(state.t0 + g.peakI * HOUR))}`, event); })
    .on('mouseleave', () => { highlight(null); tip.hide(); });
  mapGeom = { dots };
}
const haloR = d3.scaleSqrt([0, 1], [2, 20]);
function updateMap() {
  const i = (clock - state.t0) / HOUR;
  mapGeom.dots.select('.halo').attr('r', (g) => {
    const v = g.q[clamp(Math.round(i), 0, g.q.length - 1)];
    return v == null ? 0 : haloR(clamp((v - g.minQ) / (g.maxQ - g.minQ || 1), 0, 1));
  });
}
function highlight(g) {
  mapGeom.dots.select('.core').attr('r', (d) => (d === g ? 7 : 3)).attr('fill', (d) => (d === g ? '#fff' : '#0b1222'));
  svg.selectAll('.row-label').style('fill', (d) => (g && d === g ? '#ffb703' : null));
}

// ---------- loop & controls ----------
function setWindow(set) {
  state = prepare(set);
  const t = new URLSearchParams(location.search).get('t');
  clock = t ? Date.parse(t) : state.t0 + Math.max(0, state.oPeak - 72) * HOUR;
  if (!t && set.label.startsWith('Last')) clock = state.t0;
  drawRidges();
  drawMap();
}
let lastT = performance.now();
function frame(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000);
  lastT = now;
  if (playing) {
    clock += dt * 10 * HOUR;
    if (clock > state.t0 + (state.n - 1) * HOUR) clock = state.t0;
  }
  document.getElementById('clock').textContent = fmtET(new Date(clock));
  updateRidges();
  requestAnimationFrame(frame);
}
const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('window').onchange = async (e) => {
  sets[e.target.value] ??= await loadJSON(`gauges-${e.target.value}.json`);
  setWindow(sets[e.target.value]);
};
document.getElementById('order').onchange = (e) => { order = e.target.value; drawRidges(); };
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });
window.addEventListener('resize', () => { drawRidges(); drawMap(); });
setWindow(sets.storm);
requestAnimationFrame(frame);
