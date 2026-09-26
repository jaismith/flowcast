import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, fmtET, HOUR, DAY, clamp } from '../shared/common.js';

mountTopbar('Forecast Fan', 'Uncertainty that unfurls as the horizon rolls forward');

const [fc, glofas, gefs] = await Promise.all([
  loadJSON('forecast-flowcast.json'),
  loadJSON('forecast-glofas.json'),
  loadJSON('forecast-gfs-ensemble.json'),
]);
const F = fc.forecast;
mountSource(`flowcast forecast issued ${fmtET(new Date(F.origin_timestamp * 1000))} ET (read-only GET of api.flowcast.jaismith.dev/forecast, cached ${fc.fetchedAt.slice(0, 10)}). GEFS: Open-Meteo Ensemble API. GloFAS v4: Open-Meteo Flood API, grid cell ${glofas.cell.lat}, ${glofas.cell.lon}.`);

const Z90 = 1.6449;
const Z50 = 0.6745;
const origin = new Date(F.origin_timestamp * 1000);
const hist = F.historical.timestamps.map((t, i) => ({ t: new Date(t * 1000), streamflow: F.historical.streamflow[i], watertemp: F.historical.watertemp[i], precip: F.historical.precip[i], airtemp: F.historical.airtemp[i] }));
const TARGETS = {
  streamflow: { label: 'Streamflow', unit: 'cfs', fmt: (v) => `${d3.format(',.0f')(v)} cfs`, log: true, color: '#4cc9f0' },
  watertemp: { label: 'Water temperature', unit: '°F', fmt: (v) => `${v.toFixed(1)} °F`, log: false, color: '#ffb703' },
};
function forecastRows(key) {
  const wf = F.water_forecast[key];
  return wf.timestamps.map((t, i) => {
    const med = wf.values[i];
    const lo = wf.confidence_intervals['5th'][i];
    const hi = wf.confidence_intervals['95th'][i];
    // Inner 50% band inferred from the 90% band assuming a (log-)normal spread.
    const tr = TARGETS[key].log ? Math.log : (v) => v;
    const inv = TARGETS[key].log ? Math.exp : (v) => v;
    const sLo = (tr(med) - tr(Math.max(lo, 1e-3))) / Z90;
    const sHi = (tr(hi) - tr(med)) / Z90;
    return { t: new Date(t * 1000), h: i + 1, med, lo, hi, q25: inv(tr(med) - sLo * Z50), q75: inv(tr(med) + sHi * Z50), sLo, sHi, precip: F.atmospheric_forecast.precip[i], airtemp: F.atmospheric_forecast.airtemp[i] };
  });
}

let target = 'streamflow';
let horizon = 0;
let playing = true;

// ---------- fan chart ----------
const fanSvg = d3.select('#fan svg');
function drawFan() {
  const node = fanSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 54, r: 90, t: 12, b: 62 };
  const T = TARGETS[target];
  const rows = forecastRows(target);
  const shown = rows.filter((r) => r.h <= horizon);
  const x = d3.scaleUtc([hist[0].t, rows[rows.length - 1].t], [m.l, w - m.r]);
  const all = [...hist.map((d) => d[target]), ...rows.flatMap((r) => [r.lo, r.hi])].filter((v) => v != null);
  const y = (T.log ? d3.scaleLog() : d3.scaleLinear()).domain(T.log ? [d3.min(all) * 0.85, d3.max(all) * 1.12] : [d3.min(all) - 1, d3.max(all) + 1]).range([h - m.b, m.t]);
  if (!T.log) y.nice();
  fanSvg.selectAll('*').remove();
  const defs = fanSvg.append('defs');
  const glow = defs.append('filter').attr('id', 'glow').attr('x', '-50%').attr('y', '-50%').attr('width', '200%').attr('height', '200%');
  glow.append('feGaussianBlur').attr('stdDeviation', 4).attr('result', 'b');
  const merge = glow.append('feMerge');
  merge.append('feMergeNode').attr('in', 'b');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');
  const fadeGrad = defs.append('linearGradient').attr('id', 'bandfade').attr('x1', 0).attr('x2', 1);
  fadeGrad.append('stop').attr('offset', '0%').attr('stop-color', T.color).attr('stop-opacity', 0.5);
  fadeGrad.append('stop').attr('offset', '100%').attr('stop-color', T.color).attr('stop-opacity', 0.18);

  fanSvg.append('g').attr('class', 'grid').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5, T.log ? '~s' : '.0f').tickSize(-(w - m.l - m.r)).tickFormat(''));
  // day bands
  const days = d3.utcDay.range(hist[0].t, rows[rows.length - 1].t);
  fanSvg.append('g').selectAll('rect').data(days.filter((_, i) => i % 2 === 0)).join('rect')
    .attr('x', (d) => x(d)).attr('width', (d) => x(d3.utcDay.offset(d, 1)) - x(d)).attr('y', m.t).attr('height', h - m.b - m.t).attr('fill', 'rgba(255,255,255,0.018)');
  fanSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(d3.utcDay.every(1)).tickFormat(d3.utcFormat('%a %-d')).tickSizeOuter(0));
  fanSvg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5, T.log ? '~s' : '.0f'));
  fanSvg.append('text').attr('x', m.l + 4).attr('y', m.t + 10).style('font-size', '11px').text(`${T.label} (${T.unit}${T.log ? ', log scale' : ''})`);

  // origin line
  fanSvg.append('line').attr('x1', x(origin)).attr('x2', x(origin)).attr('y1', m.t).attr('y2', h - m.b).attr('stroke', 'rgba(255,255,255,0.35)').attr('stroke-dasharray', '2 3');
  fanSvg.append('text').attr('x', x(origin) - 6).attr('y', h - m.b - 8).attr('text-anchor', 'end').style('font-size', '11px').text('observed ◂');
  fanSvg.append('text').attr('x', x(origin) + 6).attr('y', h - m.b - 8).style('font-size', '11px').text('▸ forecast');

  const histLine = d3.line().defined((d) => d[target] != null).x((d) => x(d.t)).y((d) => y(d[target]));
  fanSvg.append('path').attr('d', histLine(hist)).attr('fill', 'none').attr('stroke', '#e3ebf6').attr('stroke-width', 1.6);
  const pts = [{ t: origin, med: hist[hist.length - 1][target], lo: hist[hist.length - 1][target], hi: hist[hist.length - 1][target], q25: hist[hist.length - 1][target], q75: hist[hist.length - 1][target] }, ...shown];
  const area = (a, b) => d3.area().x((d) => x(d.t)).y0((d) => y(d[a])).y1((d) => y(d[b])).curve(d3.curveMonotoneX);
  fanSvg.append('path').attr('d', area('lo', 'hi')(pts)).attr('fill', 'url(#bandfade)').attr('opacity', 0.55);
  fanSvg.append('path').attr('d', area('q25', 'q75')(pts)).attr('fill', T.color).attr('opacity', 0.35);
  fanSvg.append('path').attr('d', d3.line().x((d) => x(d.t)).y((d) => y(d.med)).curve(d3.curveMonotoneX)(pts)).attr('fill', 'none').attr('stroke', T.color).attr('stroke-width', 2.2).attr('filter', 'url(#glow)');

  // Leading-edge distribution slice
  const cur = shown[shown.length - 1];
  if (cur) {
    const cx = x(cur.t);
    const tr = T.log ? Math.log : (v) => v;
    const inv = T.log ? Math.exp : (v) => v;
    const ys = d3.range(-3, 3.01, 0.05).map((z) => {
      const s = z < 0 ? cur.sLo : cur.sHi;
      return { v: inv(tr(cur.med) + z * s), dens: Math.exp(-0.5 * z * z) };
    });
    const vw = 46;
    const violin = d3.area().y((d) => y(d.v)).x0(cx).x1((d) => cx + d.dens * vw).curve(d3.curveBasis);
    fanSvg.append('path').attr('d', violin(ys)).attr('fill', T.color).attr('opacity', 0.28).attr('stroke', T.color).attr('stroke-width', 1);
    fanSvg.append('line').attr('x1', cx).attr('x2', cx).attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703').attr('stroke-width', 1.2).attr('filter', 'url(#glow)');
    fanSvg.append('circle').attr('cx', cx).attr('cy', y(cur.med)).attr('r', 4.5).attr('fill', '#fff').attr('filter', 'url(#glow)');
    const lab = fanSvg.append('g').attr('class', 'band-label');
    lab.append('text').attr('x', cx + vw + 6).attr('y', y(cur.hi)).attr('dy', '0.35em').style('fill', '#c9d6ea').text(`95th ${T.fmt(cur.hi)}`);
    lab.append('text').attr('x', cx + vw + 6).attr('y', y(cur.med)).attr('dy', '0.35em').style('fill', '#fff').style('font-weight', 600).text(`median ${T.fmt(cur.med)}`);
    lab.append('text').attr('x', cx + vw + 6).attr('y', y(cur.lo)).attr('dy', '0.35em').style('fill', '#c9d6ea').text(`5th ${T.fmt(cur.lo)}`);
  }

  // Forcing strip: precipitation bars + air temp
  const sy0 = h - m.b + 26;
  const sy1 = h - 2;
  const allAtm = [...hist.map((d) => ({ t: d.t, p: d.precip, a: d.airtemp })), ...rows.map((r) => ({ t: r.t, p: r.precip, a: r.airtemp, fc: true }))];
  const yp = d3.scaleLinear([0, Math.max(0.08, d3.max(allAtm, (d) => d.p))], [0, sy1 - sy0]);
  const ya = d3.scaleLinear(d3.extent(allAtm, (d) => d.a), [sy1, sy0]);
  fanSvg.append('g').selectAll('rect').data(allAtm.filter((d) => d.p > 0)).join('rect')
    .attr('x', (d) => x(d.t) - 1).attr('width', 2).attr('y', sy0).attr('height', (d) => yp(d.p)).attr('fill', (d) => (d.fc ? '#9aa6ff' : '#5f6aa8'));
  fanSvg.append('path').attr('d', d3.line().x((d) => x(d.t)).y((d) => ya(d.a))(allAtm)).attr('fill', 'none').attr('stroke', 'rgba(255,183,3,0.6)').attr('stroke-width', 1);
  fanSvg.append('text').attr('x', w - m.r + 6).attr('y', sy0 + 8).style('font-size', '10px').style('fill', '#9aa6ff').text('precip (in)');
  fanSvg.append('text').attr('x', w - m.r + 6).attr('y', sy0 + 20).style('font-size', '10px').style('fill', 'rgba(255,183,3,0.8)').text('air temp');
  fanSvg.append('text').attr('x', w - m.r + 6).attr('y', sy0 + 32).style('font-size', '10px').text('model inputs');

  document.getElementById('fan-hint').textContent = `Observed last 10 days, then forecast median, 90% band (model quantiles), and 50% band (inferred assuming ${T.log ? 'log-normal' : 'normal'} spread). At +1 h the 90% band is already ${T.fmt(rows[0].hi - rows[0].lo)} wide.`;
  return { cur, rows };
}

// ---------- GEFS plume ----------
const plumeSvg = d3.select('#plume svg');
const gTimes = gefs.time.map((t) => new Date(`${t}Z`));
function drawPlume() {
  const node = plumeSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 40, r: 12, t: 10, b: 24 };
  const isRain = target === 'streamflow';
  const series = (isRain ? gefs.precipitation : gefs.temperature).map((mem) => {
    if (!isRain) return mem;
    let acc = 0;
    return mem.map((v) => (acc += v ?? 0));
  });
  const end = new Date(origin.getTime() + 168 * HOUR);
  const idxEnd = gTimes.findIndex((t) => t > end);
  const x = d3.scaleUtc([gTimes[0], end], [m.l, w - m.r]);
  const y = d3.scaleLinear(isRain ? [0, d3.max(series, (s) => s[idxEnd - 1]) * 1.1 || 1] : d3.extent(series.flat()), [h - m.b, m.t]).nice();
  const cursorT = new Date(origin.getTime() + horizon * HOUR);
  const ci = clamp(gTimes.findIndex((t) => t >= cursorT), 0, gTimes.length - 1);
  plumeSvg.selectAll('*').remove();
  plumeSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(x).ticks(4).tickFormat(d3.utcFormat('%a %-d')).tickSizeOuter(0));
  plumeSvg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5));
  const line = d3.line().defined((_, i) => i <= Math.max(ci, 0) && i < idxEnd).x((_, i) => x(gTimes[i])).y((v) => y(v));
  const qs = [0.1, 0.5, 0.9].map((q) => gTimes.slice(0, idxEnd).map((_, i) => d3.quantile(series.map((s) => s[i]), q)));
  plumeSvg.append('path').attr('d', d3.area().defined((_, i) => i <= ci).x((_, i) => x(gTimes[i])).y0((_, i) => y(qs[0][i])).y1((_, i) => y(qs[2][i]))(qs[0])).attr('fill', isRain ? 'rgba(123,140,255,0.22)' : 'rgba(255,183,3,0.18)');
  plumeSvg.append('g').selectAll('path').data(series).join('path').attr('d', line).attr('fill', 'none').attr('stroke', isRain ? 'rgba(154,166,255,0.45)' : 'rgba(255,183,3,0.4)').attr('stroke-width', 0.8);
  plumeSvg.append('path').attr('d', line(qs[1])).attr('fill', 'none').attr('stroke', '#fff').attr('stroke-width', 1.8);
  plumeSvg.append('line').attr('x1', x(cursorT)).attr('x2', x(cursorT)).attr('y1', m.t).attr('y2', h - m.b).attr('stroke', '#ffb703');
  plumeSvg.append('text').attr('x', m.l + 4).attr('y', m.t + 10).style('font-size', '11px').text(isRain ? 'inches, cumulative' : '°F, 2 m air temperature');
  const vals = series.map((s) => s[ci]);
  if (isRain) {
    const p50 = d3.median(vals);
    const pOver = vals.filter((v) => v > 0.5).length / vals.length;
    document.getElementById('r-rain').textContent = `${p50.toFixed(2)} in · ${(pOver * 100).toFixed(0)}% >½″`;
  } else {
    document.getElementById('r-rain').textContent = `air ${d3.median(vals).toFixed(0)} °F`;
  }
  document.getElementById('plume-title').textContent = isRain ? 'GEFS precipitation plume' : 'GEFS air-temperature plume';
  document.getElementById('plume-hint').textContent = `${isRain ? 'Cumulative precipitation' : '2 m air temperature'} at the basin centroid, ${series.length} ensemble members; band = 10–90%`;
}

// ---------- GloFAS ridgeline ----------
const ridgeSvg = d3.select('#ridge svg');
const gfTimes = glofas.time.map((t) => new Date(`${t}T12:00:00Z`));
const todayIdx = gfTimes.findIndex((t) => t >= d3.utcDay.floor(origin));
const fcDays = d3.range(todayIdx, gfTimes.length).map((i) => ({ t: gfTimes[i], vals: glofas.members.map((m) => m[i]).filter((v) => v != null) }));
const lastObs = hist.filter((d) => d.streamflow != null).at(-1).streamflow;
let ridgeT0 = null;
function drawRidge(now) {
  const node = ridgeSvg.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 54, r: 20, t: 10, b: 22 };
  const allVals = fcDays.flatMap((d) => d.vals).concat([lastObs]).sort(d3.ascending);
  const y = d3.scaleLog([d3.quantile(allVals, 0.003) * 0.8, d3.quantile(allVals, 0.998) * 1.15], [h - m.b, m.t]);
  const n = fcDays.length;
  const x = d3.scaleBand(d3.range(n), [m.l, w - m.r]).paddingInner(0);
  const ridgeW = x.bandwidth() * 2.1;
  const grid = d3.range(0, 1.0001, 1 / 120).map((f) => Math.exp(Math.log(y.domain()[0]) + f * (Math.log(y.domain()[1]) - Math.log(y.domain()[0]))));
  const kde = (vals) => {
    const lv = vals.map(Math.log);
    const sd = d3.deviation(lv) || 0.05;
    const bw = Math.max(0.05, 1.06 * sd * Math.pow(lv.length, -0.2));
    return grid.map((g) => { const lg = Math.log(g); return [g, d3.mean(lv, (v) => Math.exp(-0.5 * ((lg - v) / bw) ** 2))]; });
  };
  const color = d3.scaleSequentialLog([d3.min(fcDays, (d) => d3.median(d.vals)), d3.max(fcDays, (d) => d3.median(d.vals))], d3.interpolateRgbBasis(['#4361ee', '#4cc9f0', '#80ffdb', '#ffd166', '#fb8500']));
  const curDay = Math.floor(horizon / 24);
  const age = ridgeT0 == null ? 1e9 : now - ridgeT0;
  ridgeSvg.selectAll('*').remove();
  ridgeSvg.append('g').attr('class', 'grid').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5, '~s').tickSize(-(w - m.l - m.r)).tickFormat(''));
  ridgeSvg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5, '~s'));
  ridgeSvg.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`)
    .call(d3.axisBottom(x).tickValues(d3.range(0, n, 2)).tickFormat((i) => (i === 0 ? 'today' : d3.utcFormat('%b %-d')(fcDays[i].t))).tickSizeOuter(0));
  ridgeSvg.append('line').attr('x1', m.l).attr('x2', w - m.r).attr('y1', y(lastObs)).attr('y2', y(lastObs)).attr('stroke', 'rgba(255,255,255,0.5)').attr('stroke-dasharray', '4 3');
  ridgeSvg.append('text').attr('x', w - m.r - 4).attr('y', y(lastObs) - 5).attr('text-anchor', 'end').style('font-size', '10.5px').text(`latest observed ${d3.format(',.0f')(lastObs)} cfs`);
  const fcRows = forecastRows('streamflow');
  const medians = [];
  fcDays.forEach((d, i) => {
    const x0 = x(i) + 2;
    const k = kde(d.vals);
    const maxD = d3.max(k, (p) => p[1]) || 1;
    const reveal = clamp((age - i * 45) / 450, 0, 1);
    const sc = (ridgeW / maxD) * reveal * (i === curDay ? 1.25 : 1);
    ridgeSvg.append('path')
      .attr('d', d3.area().y((p) => y(p[0])).x0(x0).x1((p) => x0 + p[1] * sc).curve(d3.curveBasis)(k))
      .attr('fill', color(d3.median(d.vals)))
      .attr('fill-opacity', i === curDay ? 0.95 : 0.6)
      .attr('stroke', i === curDay ? '#fff' : '#060a13')
      .attr('stroke-width', i === curDay ? 1.4 : 0.8);
    medians.push([x0, y(d3.median(d.vals))]);
  });
  ridgeSvg.append('path').attr('d', d3.line()(medians)).attr('fill', 'none').attr('stroke', 'rgba(255,255,255,0.55)').attr('stroke-width', 1).attr('stroke-dasharray', '1 2');
  for (let i = 1; i <= 7 && i < n; i++) {
    const r = fcRows[Math.min(fcRows.length - 1, i * 24 - 12)];
    ridgeSvg.append('circle').attr('cx', x(i) + 2).attr('cy', y(r.med)).attr('r', 3.5).attr('fill', '#fff').attr('stroke', '#4cc9f0').attr('stroke-width', 1.5);
  }
  ridgeSvg.append('text').attr('x', m.l + 6).attr('y', m.t + 10).style('font-size', '11px').text('Discharge (cfs, log) · white dots: flowcast median · dotted: GloFAS median');
}

// ---------- readouts & loop ----------
function updateReadout(cur) {
  const T = TARGETS[target];
  document.getElementById('hlabel').textContent = `+${horizon} h`;
  document.getElementById('horizon').value = horizon;
  if (!cur) {
    document.getElementById('r-time').textContent = fmtET(origin);
    document.getElementById('r-med').textContent = '–';
    document.getElementById('r-band').textContent = '–';
    return;
  }
  document.getElementById('r-time').textContent = fmtET(cur.t);
  document.getElementById('r-med').textContent = T.fmt(cur.med);
  document.getElementById('r-band').textContent = `${T.fmt(cur.lo).replace(/ .*/, '')}–${T.fmt(cur.hi)}`;
}

function redraw(now = performance.now()) {
  const { cur } = drawFan();
  drawPlume();
  drawRidge(now);
  updateReadout(cur);
}

let hold = 0;
let lastT = performance.now();
let acc = 0;
function tick(now) {
  const dt = (now - lastT) / 1000;
  lastT = now;
  if (playing) {
    if (horizon >= 168) {
      hold += dt;
      if (hold > 2.2) { horizon = 0; hold = 0; }
    } else {
      acc += dt * 22;
      const inc = Math.floor(acc);
      if (inc) { horizon = Math.min(168, horizon + inc); acc -= inc; }
    }
  }
  redraw(now);
  requestAnimationFrame(tick);
}

const playBtn = document.getElementById('play');
playBtn.onclick = () => { playing = !playing; playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play'; };
document.getElementById('horizon').oninput = (e) => {
  horizon = +e.target.value;
  if (playing) playBtn.click();
};
for (const key of Object.keys(TARGETS)) {
  document.getElementById(`t-${key}`).onclick = () => {
    target = key;
    for (const k of Object.keys(TARGETS)) document.getElementById(`t-${k}`).classList.toggle('active', k === key);
  };
}
window.addEventListener('keydown', (e) => { if (e.code === 'Space') { e.preventDefault(); playBtn.click(); } });
const params = new URLSearchParams(location.search);
if (params.has('h')) { horizon = +params.get('h'); playing = false; playBtn.textContent = '▶ Play'; }
if (params.get('target') === 'watertemp') document.getElementById('t-watertemp').click();
ridgeT0 = performance.now();
requestAnimationFrame(tick);
