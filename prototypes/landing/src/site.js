import './styles.css';
import * as d3 from 'd3';
import { evolutionChart, fanChart, nwsChart, precipStrip, recentChart, reliabilityChart, skillChart, sweChart } from './lib/charts.js';
import {
  TZ, dayOfYear, fmt, fmtFlow, fmtPct, fmtTemp, initUnitToggle, loadJSON, onResize, pctBadge,
  renderFooter, renderNav, segmented, siteFromLocation, tempDelta, units,
} from './lib/common.js';
import { RAIN_STOPS, ROLE_COLORS, ROLE_LABELS, addBasinLayers, createMap, setBasinRain, setLayerGroup } from './lib/map.js';

const SITE = siteFromLocation();
const H = 3600 * 1000;
const USGS_LATEST = 'https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items';

initUnitToggle();
renderNav(SITE);

const state = { issueIdx: 0, issueList: [], nwsOnly: false, recentVar: 'flow', rainOnMap: false, map: null, mapReady: false };

async function main() {
  const [meta, geo, recent, clim, skill, overview] = await Promise.all([
    loadJSON(`sites/${SITE}/meta.json`), loadJSON(`sites/${SITE}/geo.json`), loadJSON(`sites/${SITE}/recent.json`),
    loadJSON(`sites/${SITE}/climatology.json`), loadJSON(`sites/${SITE}/skill.json`), loadJSON('sites.json'),
  ]);
  document.title = `${meta.name} · flowcast`;
  const ov = overview.sites.find((s) => s.id === SITE) || {};
  state.geo = geo;
  renderHero(meta, recent, clim, ov);
  renderMap(meta, geo);
  renderRecent(recent, clim);
  renderSkill(meta, skill);
  renderHeroLinks(meta, geo, skill, null);
  renderFooter(`<p><b>Model.</b> Flow: flowcast's final three-seed LSTM ensemble (11 GEFS weather members, 132 samples per forecast) with calibrated spread. Water temperature: model v1, two seeds, 40 members, calibrated. Scores use every forecast issued at 00, 06, 12 and 18 UTC in Oct 2020 – Sep 2022 (fair CRPS; 95% intervals from 7-day block bootstrap).</p>`);
  tabsSpy();
  refreshLive(meta, clim);

  const [hc, obs] = await Promise.all([loadJSON(`sites/${SITE}/hindcast.json`), loadJSON(`sites/${SITE}/observed.json`)]);
  renderReplay(meta, hc, obs);
  renderBasin(meta, geo, obs);
  renderHeroLinks(meta, geo, skill, hc);
  units.onChange(() => {
    renderHero(meta, recent, clim, ov);
    renderRecent(recent, clim);
    drawReplay(meta, hc, obs);
    renderBasin(meta, geo, obs);
    renderSkill(meta, skill);
  });
}

// ---------------------------------------------------------------------------------------------- hero

let live = null;

function lastValid(s) {
  for (let i = s.v.length - 1; i >= 0; i--) if (s.v[i] != null) return { t: new Date(s.t[i] * 1000), v: s.v[i], i };
  return null;
}

function valueAgo(s, t, hours) {
  const target = t / 1000 - hours * 3600;
  const i = d3.bisectLeft(s.t, target);
  return i < s.t.length && Math.abs(s.t[i] - target) < 3 * 3600 ? s.v[i] : null;
}

function approxPct(value, b) {
  if (value == null || !b || b.some((x) => x == null)) return null;
  const qs = [0.1, 0.25, 0.5, 0.75, 0.9];
  if (value <= b[0]) return Math.max(0.02, 0.1 * (value / Math.max(b[0], 1e-6)));
  if (value >= b[4]) return Math.min(0.98, 0.9 + 0.08 * Math.min(1, (value - b[4]) / Math.max(b[4], 1e-6)));
  for (let k = 0; k < 4; k++) if (value <= b[k + 1]) return qs[k] + ((value - b[k]) / Math.max(b[k + 1] - b[k], 1e-9)) * (qs[k + 1] - qs[k]);
  return null;
}

function climBand(clim, key, t) {
  const doy = dayOfYear(t);
  const arr = clim[key];
  const k = (Math.min(doy, 366) - 1) * 5;
  const b = arr.slice(k, k + 5);
  return b.every((x) => x != null) ? b : null;
}

function sparkline(s, color) {
  const pts = s.t.map((t, i) => [t, s.v[i]]).filter((p) => p[1] != null).slice(-24 * 7);
  if (pts.length < 2) return '';
  const x = d3.scaleLinear().domain(d3.extent(pts, (p) => p[0])).range([0, 130]);
  const y = d3.scaleLinear().domain(d3.extent(pts, (p) => p[1])).range([26, 2]);
  return `<svg width="130" height="28" viewBox="0 0 130 28" aria-hidden="true"><path d="${d3.line().x((p) => x(p[0])).y((p) => y(p[1]))(pts)}" fill="none" stroke="${color}" stroke-width="1.6"/></svg>`;
}

function trend(s, latest) {
  const prev = valueAgo(s, latest.t, 24);
  if (prev == null || prev === 0) return '';
  const r = latest.v / prev - 1;
  if (Math.abs(r) < 0.03) return '<span>steady over 24 h</span>';
  return r > 0 ? `<span class="trend-up">▲ up ${Math.round(r * 100)}% in 24 h</span>` : `<span class="trend-down">▼ down ${Math.round(-r * 100)}% in 24 h</span>`;
}

function renderHero(meta, recent, clim, ov) {
  document.getElementById('site-kind').textContent = meta.kind;
  document.getElementById('site-title').textContent = meta.name;
  document.getElementById('site-tagline').textContent = meta.tagline;
  const flow = lastValid(recent.flow);
  const temp = lastValid(recent.temp);
  const stage = lastValid(recent.stage);
  const qNow = live?.flow ?? flow;
  const tNow = live?.temp ?? temp;
  const sNow = live?.stage ?? stage;
  const qBand = qNow ? climBand(clim, 'flow', qNow.t) : null;
  const pct = live?.flow ? approxPct(qNow.v, qBand) : ov.pct ?? approxPct(qNow?.v, qBand);
  const tBand = tNow ? climBand(clim, 'temp', tNow.t) : null;
  const cards = [];
  cards.push(`<div class="now-card"><span class="l">Flow</span><span class="v">${qNow ? fmtFlow(qNow.v, false) : '–'}<small>ft³/s</small></span>
    <span class="s">${flow ? trend(recent.flow, flow) : ''}</span><span class="s">${pctBadge(pct)}</span>${sparkline(recent.flow, '#1f7ae0')}</div>`);
  const stress = tNow && tNow.v >= 21 ? '<span class="trend-down">above the 70°F trout-stress line</span>' : '';
  cards.push(`<div class="now-card"><span class="l">Water temperature</span><span class="v">${tNow ? fmtTemp(tNow.v, 1).replace(/°.$/, '') : '–'}<small>°${units.temp}</small></span>
    <span class="s">${tBand ? `normal for the date ${fmtTemp(tBand[1], 0)}–${fmtTemp(tBand[3], 0)}` : ''}</span><span class="s">${stress}</span>${sparkline(recent.temp, '#d9480f')}</div>`);
  if (sNow && meta.flood_stage_ft) {
    const fs = meta.flood_stage_ft;
    const toAction = fs.action - sNow.v;
    cards.push(`<div class="now-card"><span class="l">River level</span><span class="v">${sNow.v.toFixed(1)}<small>ft</small></span>
      <span class="s">${toAction > 0 ? `${toAction.toFixed(1)} ft below action stage (${fs.action} ft)` : `<span class="trend-down">above action stage (${fs.action} ft)</span>`}</span><span class="s">Flood stage ${fs.minor} ft · NWS ${meta.nws_lid}</span></div>`);
  } else if (qBand) {
    cards.push(`<div class="now-card"><span class="l">Normal for today</span><span class="v">${fmtFlow(qBand[2], false)}<small>ft³/s</small></span>
      <span class="s">middle half of years: ${fmtFlow(qBand[1], false)}–${fmtFlow(qBand[3])}</span><span class="s">2000–2022 record</span></div>`);
  }
  document.getElementById('now-cards').innerHTML = cards.join('');
  const t = qNow?.t ?? tNow?.t;
  document.getElementById('now-asof').innerHTML = t ? `${live ? 'Live' : 'As of'} ${fmt.full(t)} · USGS ${meta.id}, provisional data` : '';
}

function renderHeroLinks(meta, geo, skill, hc) {
  const s24 = skill.flow.rows.find((r) => r.lead_h === 24);
  const flood = hc?.events.find((e) => e.kind === 'flood');
  const links = [
    ['#forecast', 'Forecast replay', flood ? `The ${fmt.monthYear(new Date(flood.time * 1000))} flood` : 'Real forecasts, 2020–22', flood ? `peak ${fmtFlow(flood.value)}: what the model saw coming` : 'see what the model predicted and what happened'],
    ['#skill', 'Skill', `${fmtPct(s24.skill)} smaller error`, skill.marfc ? 'than persistence at 1 day; compared with the NWS too' : 'than persistence at 1 day ahead'],
    ['#basin', 'Basin', `${Math.round(meta.area_mi2).toLocaleString()} mi² upstream`, `${meta.nid_dams} dams, ${geo.gauges.features.filter((g) => g.properties.active).length} upstream gauges`],
  ];
  document.getElementById('hero-links').innerHTML = links.map(([href, k, b, s]) => `<a class="hero-link" href="${href}"><span class="k">${k}</span><b>${b}</b><span>${s}</span></a>`).join('');
}

async function refreshLive(meta, clim) {
  const get = async (code) => {
    const r = await fetch(`${USGS_LATEST}?monitoring_location_id=USGS-${meta.id}&parameter_code=${code}&f=json`);
    if (!r.ok) throw new Error(r.status);
    const f = (await r.json()).features?.[0]?.properties;
    return f && f.value != null ? { t: new Date(f.time), v: +f.value } : null;
  };
  try {
    const [flow, temp, stage] = await Promise.all([get('00060'), get('00010').catch(() => null), get('00065').catch(() => null)]);
    if (!flow) return;
    live = { flow, temp, stage };
    const [recent, overview] = await Promise.all([loadJSON(`sites/${SITE}/recent.json`), loadJSON('sites.json')]);
    renderHero(meta, recent, clim, overview.sites.find((s) => s.id === SITE) || {});
  } catch (e) {
    console.info('live USGS refresh skipped', e);
  }
}

// ---------------------------------------------------------------------------------------------- map

function renderMap(meta, geo) {
  const map = createMap('basin-map', { bounds: [[geo.bounds[0], geo.bounds[1]], [geo.bounds[2], geo.bounds[3]]] });
  state.map = map;
  const layers = [
    ['rivers', 'Rivers', '#1462c4', true],
    ['gauges', `Upstream gauges (${geo.gauges.features.length})`, ROLE_COLORS.upstream, true],
    ['dams', `Dams (${geo.dams.features.length})`, '#6b4f3a', true],
    ['terrain', 'Terrain', '#8a9aa6', true],
    ['rain', 'Forecast rain', '#5b3cc4', false],
  ].filter(([k]) => k !== 'gauges' || geo.gauges.features.length).filter(([k]) => k !== 'dams' || geo.dams.features.length);
  const box = document.getElementById('map-layers');
  box.innerHTML = layers.map(([k, label, c, on]) => `<button class="chip" type="button" data-layer="${k}" aria-pressed="${on}"><span class="sw" style="background:${c}"></span>${label}</button>`).join('');
  box.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const on = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', String(on));
    if (b.dataset.layer === 'rain') {
      state.rainOnMap = on;
      updateMapRain();
    } else setLayerGroup(map, b.dataset.layer, on);
    renderMapLegend(geo);
  });
  map.on('load', () => {
    addBasinLayers(map, geo, { siteName: meta.short });
    state.mapReady = true;
    updateMapRain();
  });
  renderMapLegend(geo);
}

function renderMapLegend(geo) {
  const el = document.getElementById('map-legend');
  const rows = [];
  const pressed = (k) => document.querySelector(`[data-layer="${k}"]`)?.getAttribute('aria-pressed') === 'true';
  if (pressed('gauges')) {
    const roles = [...new Set(geo.gauges.features.map((f) => f.properties.role))];
    for (const r of ['below_dam', 'model_input', 'upstream'].filter((x) => roles.includes(x))) {
      const fill = r === 'upstream' ? '#fff' : ROLE_COLORS[r];
      rows.push(`<div class="legend-row"><span class="legend-swatch" style="background:${fill};border:2px solid ${r === 'upstream' ? ROLE_COLORS.upstream : '#fff'}"></span>${ROLE_LABELS[r]}</div>`);
    }
  }
  if (pressed('dams')) rows.push('<div class="legend-row"><span class="legend-swatch" style="background:#6b4f3a"></span>Dam (size = storage)</div>');
  if (state.rainOnMap) {
    const issue = state.issueList.length ? new Date(state.hc.issues[state.issueList[state.issueIdx]] * 1000) : null;
    const inch = units.temp === 'F';
    rows.push(`<div><b>7-day forecast precipitation</b>${issue ? ` from ${fmt.dayTime(issue)}` : ''}</div><div class="legend-ramp" style="background:linear-gradient(90deg,${RAIN_STOPS.map((s) => s[1]).join(',')})"></div><div class="legend-ends"><span>0</span><span>${inch ? '4.7 in' : '120 mm'}</span></div>`);
  }
  el.innerHTML = rows.join('');
}

function updateMapRain() {
  if (!state.mapReady) return;
  let total = null;
  if (state.rainOnMap && state.hc && state.issueList.length) {
    total = state.hc.precip.totals[state.issueList[state.issueIdx] * 3];
  }
  setBasinRain(state.map, state.rainOnMap ? total ?? 0 : null);
}

// ---------------------------------------------------------------------------------------------- recent

function renderRecent(recent, clim) {
  const el = document.getElementById('recent-chart');
  const draw = () => {
    const key = state.recentVar;
    const s = recent[key];
    const now = live?.flow?.t ?? new Date(recent.fetched_at);
    recentChart(el, {
      t: s.t, v: s.v, isTemp: key === 'temp', now,
      bands: (day) => climBand(clim, key, new Date(+day + 12 * H)),
    });
  };
  if (!el.dataset.bound) {
    el.dataset.bound = '1';
    segmented(document.getElementById('recent-var'), (d) => { state.recentVar = d.var; draw(); });
    onResize(el, draw);
  }
  draw();
}

// ---------------------------------------------------------------------------------------------- replay

const EVENT_LABEL = { flood: 'Flood', heat: 'Heat wave', melt: 'Snowmelt' };

function eventChips(meta, hc, obs) {
  const evs = [];
  const swe = obs.swe;
  const sweAt = (t) => swe.v[Math.round((t - swe.t0) / 86400)] ?? null;
  for (const e of hc.events) {
    if (e.kind === 'melt' && hc.events.some((f) => f.kind === 'flood' && Math.abs(f.time - e.time) < 3 * 86400)) continue;
    let label = EVENT_LABEL[e.kind];
    if (e.kind === 'flood') {
      const before = sweAt(e.time - 7 * 86400);
      const at = sweAt(e.time);
      if (before != null && at != null && before - at > 15) label = 'Rain + snowmelt flood';
      if (evs.filter((x) => x.e.kind === 'flood').length === 0) label = label === 'Flood' ? 'Biggest flood' : `Biggest flood (rain + snowmelt)`;
    }
    const valueTxt = e.kind === 'heat' ? `water peaked at ${fmtTemp(e.value, 0)}` : `peak ${fmtFlow(e.value)}`;
    evs.push({ e, label, sub: `${fmt.dayYear(new Date(e.time * 1000))} · ${valueTxt}` });
  }
  return evs;
}

function renderReplay(meta, hc, obs) {
  state.hc = hc;
  const allIdx = hc.issues.map((_, i) => i);
  const setList = () => {
    state.issueList = state.nwsOnly ? allIdx.filter((i) => hc.marfc[String(i)]) : allIdx;
  };
  setList();
  const evs = eventChips(meta, hc, obs);
  const chips = document.getElementById('event-chips');
  chips.innerHTML = evs.map((x, k) => `<button class="event-chip" type="button" data-k="${k}" aria-pressed="false"><b>${x.label}</b><span>${x.sub}</span></button>`).join('')
    + (Object.keys(hc.marfc).length ? `<button class="event-chip" type="button" id="nws-only" aria-pressed="false"><b>NWS times only</b><span>Step through forecasts with an NWS bulletin to compare</span></button>` : '');
  chips.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.id === 'nws-only') {
      const cur = state.issueList[state.issueIdx];
      state.nwsOnly = !state.nwsOnly;
      b.setAttribute('aria-pressed', String(state.nwsOnly));
      setList();
      selectIssue(nearestIdx(cur), meta, hc, obs);
      return;
    }
    const ev = evs[+b.dataset.k].e;
    chips.querySelectorAll('.event-chip:not(#nws-only)').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    let idx = ev.issue;
    if (meta.marfc && state.nwsOnly) idx = nearestIdx(idx);
    selectIssue(state.issueList.indexOf(idx) >= 0 ? state.issueList.indexOf(idx) : nearestIdx(idx), meta, hc, obs);
  });
  const range = document.getElementById('issue-range');
  range.addEventListener('input', () => selectIssue(+range.value, meta, hc, obs, false));
  document.getElementById('issue-prev').addEventListener('click', () => selectIssue(state.issueIdx - 1, meta, hc, obs));
  document.getElementById('issue-next').addEventListener('click', () => selectIssue(state.issueIdx + 1, meta, hc, obs));
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea') && e.target.id !== 'issue-range') return;
    if (!document.getElementById('forecast').matches(':hover') && document.activeElement !== range) return;
    if (e.key === 'ArrowLeft') { selectIssue(state.issueIdx - 1, meta, hc, obs); e.preventDefault(); }
    if (e.key === 'ArrowRight') { selectIssue(state.issueIdx + 1, meta, hc, obs); e.preventDefault(); }
  });
  drawScrubTrack(hc, obs, evs);
  onResize(document.getElementById('flow-fan'), () => drawReplay(meta, hc, obs));
  onResize(document.getElementById('scrub-track'), () => drawScrubTrack(hc, obs, evs));

  const fromUrl = +new URLSearchParams(location.search).get('issue');
  let start = evs.length ? evs[0].e.issue : 0;
  if (fromUrl) start = d3.bisectLeft(hc.issues, fromUrl);
  selectIssue(nearestIdx(start), meta, hc, obs);
  if (!fromUrl && evs.length) chips.querySelector('.event-chip')?.setAttribute('aria-pressed', 'true');
}

function nearestIdx(issueIndex) {
  let best = 0;
  let bd = Infinity;
  state.issueList.forEach((ii, k) => {
    const d = Math.abs(state.hc.issues[ii] - state.hc.issues[issueIndex]);
    if (d < bd) { bd = d; best = k; }
  });
  return best;
}

function selectIssue(k, meta, hc, obs, syncRange = true) {
  state.issueIdx = Math.max(0, Math.min(state.issueList.length - 1, k));
  const range = document.getElementById('issue-range');
  range.max = String(state.issueList.length - 1);
  if (syncRange) range.value = String(state.issueIdx);
  drawReplay(meta, hc, obs);
  updateMapRain();
  renderMapLegend(state.geo);
  const url = new URL(location.href);
  url.searchParams.set('issue', String(hc.issues[state.issueList[state.issueIdx]]));
  history.replaceState(null, '', url);
}

function drawScrubTrack(hc, obs, evs) {
  const el = document.getElementById('scrub-track');
  el.innerHTML = '';
  const w = el.clientWidth;
  const h = 18;
  const t0 = hc.issues[0];
  const t1 = hc.issues.at(-1);
  const x = d3.scaleLinear().domain([t0, t1]).range([8, w - 8]);
  const pts = [];
  for (let i = 0; i < obs.flow.v.length; i += 24) {
    const t = obs.flow.t0 + i * 3600;
    if (t < t0 || t > t1) continue;
    pts.push([t, d3.max(obs.flow.v.slice(i, i + 24)) ?? 0]);
  }
  const y = d3.scaleSqrt().domain([0, d3.max(pts, (p) => p[1])]).range([h, 1]);
  const svg = d3.select(el).append('svg').attr('width', w).attr('height', h);
  svg.append('path').attr('d', d3.area().x((p) => x(p[0])).y0(h).y1((p) => y(p[1]))(pts)).attr('fill', '#c9daea');
  svg.selectAll(null).data(evs).join('circle').attr('cx', (d) => x(d.e.time)).attr('cy', 4).attr('r', 3).attr('fill', (d) => (d.e.kind === 'heat' ? 'var(--temp)' : 'var(--navy)'));
}

function forecastSummary(meta, hc, obs, ii, issue) {
  const L = hc.leads.length;
  const base = ii * L * 5;
  let peak = { v: -Infinity, l: 0 };
  hc.leads.forEach((l, j) => { const v = hc.flow[base + j * 5 + 2]; if (v != null && v > peak.v) peak = { v, l, j }; });
  if (!Number.isFinite(peak.v)) return 'No flow forecast stored for this issue.';
  const lo = hc.flow[base + peak.j * 5];
  const hi = hc.flow[base + peak.j * 5 + 4];
  const i0 = Math.round((+issue / 1000 - obs.flow.t0) / 3600);
  let op = { v: -Infinity, i: 0 };
  for (let i = i0 + 1; i <= i0 + 168 && i < obs.flow.v.length; i++) if (obs.flow.v[i] != null && obs.flow.v[i] > op.v) op = { v: obs.flow.v[i], i };
  const now = obs.flow.v[i0 - 1];
  const rising = peak.v > (now ?? 0) * 1.15;
  const peakT = new Date(+issue + peak.l * H);
  const obsT = new Date((obs.flow.t0 + op.i * 3600) * 1000);
  const inside = op.v >= lo && op.v <= hi;
  const forecastTxt = rising
    ? `flow rising to about <b>${fmtFlow(peak.v)}</b> by ${fmt.dayTime(peakT)} (90% range ${fmtFlow(lo, false)}–${fmtFlow(hi)})`
    : `flow holding near or below <b>${fmtFlow(Math.max(peak.v, now ?? 0))}</b> for the week`;
  const nws = hc.marfc[String(ii)];
  let nwsTxt = '';
  if (nws) {
    const nv = d3.max(nws.v);
    nwsTxt = ` NWS bulletin peak: <b>${fmtFlow(nv)}</b>.`;
  }
  const what = Number.isFinite(op.v) ? ` What happened: ${rising || op.v > (now ?? 0) * 1.15 ? `peaked at <b>${fmtFlow(op.v)}</b> on ${fmt.dayTime(obsT)}` : `flow stayed at or below <b>${fmtFlow(op.v)}</b>`}${rising ? (inside ? ', inside the forecast range.' : op.v > hi ? ', above the forecast range.' : ', below the forecast range.') : '.'}` : '';
  return `<b>The model said:</b> ${forecastTxt}.${nwsTxt}${what}`;
}

function drawReplay(meta, hc, obs) {
  if (!state.issueList.length) return;
  const ii = state.issueList[state.issueIdx];
  const issue = new Date(hc.issues[ii] * 1000);
  const L = hc.leads.length;
  document.getElementById('issue-label').textContent = fmt.full(issue);
  document.getElementById('forecast-summary').innerHTML = forecastSummary(meta, hc, obs, ii, issue);
  const q = hc.leads.map((_, j) => hc.flow.slice((ii * L + j) * 5, (ii * L + j) * 5 + 5));
  const nws = hc.marfc[String(ii)];
  fanChart(document.getElementById('flow-fan'), { issue, leads: hc.leads, q, obs: obs.flow, nws });
  document.getElementById('flow-key').innerHTML = `<span><i style="background:var(--band-90)"></i>90% range</span><span><i style="background:var(--band-50)"></i>likely (50%)</span><span><i class="line" style="background:var(--blue)"></i>middle</span><span><i class="line" style="background:var(--obs)"></i>observed</span>${nws ? '<span><i class="dash" style="border-color:var(--nws)"></i>NWS</span>' : ''}`;
  const bins = [];
  const nb = hc.precip.bins.length / hc.issues.length / 3;
  for (let k = 0; k < nb; k++) bins.push(hc.precip.bins.slice((ii * nb + k) * 3, (ii * nb + k) * 3 + 3));
  precipStrip(document.getElementById('precip-strip'), { issue, binH: hc.precip.bin_h, bins, totals: hc.precip.totals.slice(ii * 3, ii * 3 + 3) });

  const tq = hc.leads.map((_, j) => hc.temp.slice((ii * L + j) * 5, (ii * L + j) * 5 + 5)).map((r, j) => (hc.leads[j] <= 48 ? r : [null, null, null, null, null]));
  const hasTemp = tq.some((r) => r[2] != null);
  const di = dailyIssue(hc, ii);
  const daily = di == null ? [] : dailyBoxes(hc, obs, di, new Date(hc.issues[di] * 1000));
  drawEvolution(meta, hc, obs, ii, issue);
  const tempEl = document.getElementById('temp-fan');
  if (!hasTemp && !daily.length) {
    tempEl.innerHTML = '<p class="skeleton">No water-temperature forecast stored for this issue time (they run at 00, 06, 12 and 18 UTC).</p>';
  } else {
    fanChart(tempEl, { issue, leads: hc.leads, q: tq, obs: obs.temp, isTemp: true, daily });
  }
  document.getElementById('temp-key').innerHTML = `<span><i style="background:var(--temp-90)"></i>hourly, next 2 days</span><span><i style="border:1.5px solid var(--temp);background:none"></i>daily high (likely range)</span><span><i class="dot" style="background:var(--obs)"></i>observed high</span>`;
}

/** Daily highs are forecast from the 12 UTC run only: use the latest one at most 18 h before `ii`. */
function dailyIssue(hc, ii) {
  const has = (k) => hc.tmax[k * 40 + 2] != null || hc.tmax[k * 40 + 7] != null;
  for (let k = ii; k >= 0 && hc.issues[ii] - hc.issues[k] <= 18 * 3600; k--) if (has(k)) return k;
  return null;
}

function interpQ(hc, ii, lead) {
  const L = hc.leads.length;
  const j = d3.bisectLeft(hc.leads, lead);
  if (j <= 0 || j >= L) return null;
  const a = hc.leads[j - 1];
  const b = hc.leads[j];
  const f = (lead - a) / (b - a);
  const qa = hc.flow.slice((ii * L + j - 1) * 5, (ii * L + j - 1) * 5 + 5);
  const qb = hc.flow.slice((ii * L + j) * 5, (ii * L + j) * 5 + 5);
  if (qa.some((v) => v == null) || qb.some((v) => v == null)) return null;
  return qa.map((v, k) => v + f * (qb[k] - v));
}

function drawEvolution(meta, hc, obs, ii, issue) {
  const i0 = Math.round((+issue / 1000 - obs.flow.t0) / 3600);
  let op = { v: -Infinity, i: 0 };
  for (let i = i0 + 1; i <= i0 + 168 && i < obs.flow.v.length; i++) if (obs.flow.v[i] != null && obs.flow.v[i] > op.v) op = { v: obs.flow.v[i], i };
  const el = document.getElementById('evo-chart');
  if (!Number.isFinite(op.v)) { el.innerHTML = ''; return; }
  const peakT = (obs.flow.t0 + op.i * 3600) * 1000;
  const points = [];
  hc.issues.forEach((t, k) => {
    if (hc.is_marfc_time[k]) return;
    const hb = (peakT - t * 1000) / H;
    if (hb < 1 || hb > 168) return;
    const q = interpQ(hc, k, hb);
    if (q) points.push({ issue: new Date(t * 1000), hBefore: hb, q, k });
  });
  document.getElementById('evo-title').textContent = `How the forecast for ${fmt.dayTime(new Date(peakT))} changed as it got closer`;
  evolutionChart(el, {
    points, peak: op.v, peakTime: new Date(peakT), current: hc.is_marfc_time[ii] ? null : issue,
    onPick: (t) => {
      const k = hc.issues.indexOf(+t / 1000);
      const pos = state.issueList.indexOf(k);
      selectIssue(pos >= 0 ? pos : nearestIdx(k), meta, hc, obs);
    },
  });
}

function dailyBoxes(hc, obs, ii, issue) {
  const out = [];
  const tmaxMap = new Map(obs.tmax.d.map((d, i) => [d, obs.tmax.v[i]]));
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: TZ, year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(issue);
  const [y, m, d] = ['year', 'month', 'day'].map((p) => +parts.find((x) => x.type === p).value);
  const offsetH = (Date.UTC(y, m - 1, d, 12) - +new Date(new Date(Date.UTC(y, m - 1, d, 12)).toLocaleString('en-US', { timeZone: TZ }) + ' UTC')) / H;
  for (let k = 0; k < 8; k++) {
    const qv = hc.tmax.slice(ii * 40 + k * 5, ii * 40 + k * 5 + 5);
    if (qv.some((v) => v == null)) continue;
    const midnightUtc = Date.UTC(y, m - 1, d + k);
    out.push({ t0: new Date(midnightUtc + (11 - offsetH) * H), t1: new Date(midnightUtc + (19 - offsetH) * H), q: qv, obs: tmaxMap.get(midnightUtc / 1000) ?? null });
  }
  return out;
}

// ---------------------------------------------------------------------------------------------- skill

function renderSkill(meta, skill) {
  const f = skill.flow.rows;
  const at = (l) => f.find((r) => r.lead_h === l);
  const t1d = skill.temp_daily.rows.find((r) => r.lead_day === 1);
  const callouts = [
    `<div class="callout"><div class="big good">${fmtPct(at(24).skill)}</div><p>smaller flow error than assuming the river stays where it is, 1 day ahead (${fmtPct(at(72).skill)} at 3 days, ${fmtPct(at(168).skill)} at 7).</p></div>`,
    `<div class="callout"><div class="big">${fmtPct(at(24).cover90)}</div><p>of the time the 90% range caught the observed flow 1 day ahead. A well-calibrated range would catch it 90% of the time.</p></div>`,
  ];
  if (skill.marfc) {
    const m24 = skill.marfc.rows.find((r) => r.lead_h === 24);
    callouts.push(`<div class="callout"><div class="big ${m24.skill_lo > 0 ? 'good' : ''}">${m24.skill >= 0 ? '+' : ''}${Math.round(m24.skill * 100)}%</div><p>vs the National Weather Service river forecast 1 day ahead (95% interval ${Math.round(m24.skill_lo * 100)} to ${Math.round(m24.skill_hi * 100)}%), over ${skill.marfc.issues} NWS bulletins.</p></div>`);
  } else {
    callouts.push(`<div class="callout"><div class="big">±${fmtFlow(at(24).mae_median, false)}</div><p>ft³/s: the typical miss of the middle forecast 1 day ahead (median flow here is ${fmtFlow(meta.median_flow_cfs)}).</p></div>`);
  }
  if (t1d) callouts.push(`<div class="callout"><div class="big">±${tempDelta(t1d.mae_median).toFixed(1)}°</div><p>typical miss of tomorrow's high water temperature (°${units.temp}), ${fmtPct(t1d.skill)} better than "same as today".</p></div>`);
  document.getElementById('skill-callouts').innerHTML = callouts.join('');

  const flowEl = document.getElementById('skill-flow');
  const draw = () => {
    skillChart(flowEl, { flow: f, temp: skill.temp.rows, national: skill.national_flow });
    const nwsEl = document.getElementById('skill-nws');
    if (skill.marfc) nwsChart(nwsEl, skill.marfc.rows);
    else reliabilityChart(nwsEl, f);
  };
  document.getElementById('skill-flow-note').textContent = 'Skill vs persistence: 1 − forecast CRPS ÷ the error of holding the last reading. Water temperature uses the same hour of the latest observed day. The shaded band is the 95% interval.';
  if (skill.marfc) {
    document.getElementById('skill-nws-title').textContent = 'Versus the National Weather Service (MARFC)';
    document.getElementById('skill-nws-note').textContent = `Each NWS bulletin (Oct 2020 – Sep 2022) against flowcast issued at the same moment. Above zero = flowcast's error is smaller; dark bars have a 95% interval above zero. NWS bulletins are single values, scored by absolute error; flowcast by CRPS.`;
  } else {
    document.getElementById('skill-nws-title').textContent = 'Are the forecast ranges honest?';
    document.getElementById('skill-nws-note').textContent = 'How often the observed flow fell inside the forecast range, by lead time. The NWS has no river forecast point here, so persistence is the benchmark.';
  }
  if (!flowEl.dataset.bound) { flowEl.dataset.bound = '1'; onResize(flowEl, draw); }
  draw();
}

// ---------------------------------------------------------------------------------------------- basin

function renderBasin(meta, geo, obs) {
  document.getElementById('site-about').textContent = meta.about;
  const imperial = units.temp === 'F';
  const area = imperial ? `${Math.round(meta.area_mi2).toLocaleString()} mi²` : `${Math.round(meta.area_km2).toLocaleString()} km²`;
  const elev = imperial ? `${Math.round(meta.elevation_m * 3.281).toLocaleString()} ft` : `${Math.round(meta.elevation_m)} m`;
  const precip = imperial ? `${(meta.precip_mm_yr / 25.4).toFixed(0)} in` : `${Math.round(meta.precip_mm_yr)} mm`;
  const rb = meta.flashiness_rb;
  const rbWords = rb > 0.6 ? 'very flashy' : rb > 0.3 ? 'moderately flashy' : 'steady';
  const active = geo.gauges.features.filter((g) => g.properties.active).length;
  const facts = [
    ['Drainage area', area, `USGS ${meta.id}`],
    ['Mean elevation', elev, `${Math.round(meta.slope_deg)}° mean slope`],
    ['Land cover', `${Math.round(meta.forest_frac * 100)}% forest`, `${Math.round(meta.developed_frac * 100)}% developed`],
    ['Precipitation', `${precip} / yr`, `${Math.round(meta.snow_frac * 100)}% falls as snow`],
    ['Typical flow', fmtFlow(meta.median_flow_cfs), `mean ${fmtFlow(meta.mean_flow_cfs, false)}; top 1% of hours above ${fmtFlow(meta.q99_cfs, false)}`],
    ['Flashiness', rbWords, `Richards–Baker index ${rb.toFixed(2)}`],
    ['Travel time to gauge', `${Math.round(meta.travel_time_mean_h)} h average`, `up to ${Math.round(meta.travel_time_max_h)} h from the far edge`],
    ['Dams', `${meta.nid_dams} in the basin`, meta.nid_major_dams ? `${meta.nid_major_dams} large` : 'none large'],
    ['Upstream gauges', `${active} reporting`, 'USGS stream gauges above this one'],
    ['Highest flow, 2020–22', fmtFlow(meta.record_validation_cfs), 'in the replay years'],
  ];
  document.getElementById('site-facts').innerHTML = facts.map(([k, v, s]) => `<div><dt>${k}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');
  const el = document.getElementById('swe-chart');
  const draw = () => sweChart(el, { swe: obs.swe, flow: obs.flow });
  if (!el.dataset.bound) { el.dataset.bound = '1'; onResize(el, draw); }
  draw();
}

// ---------------------------------------------------------------------------------------------- tabs

function tabsSpy() {
  const links = [...document.querySelectorAll('.tabs a')];
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) links.forEach((a) => a.classList.toggle('active', a.getAttribute('href') === `#${e.target.id}`));
    }
  }, { rootMargin: '-45% 0px -50% 0px' });
  links.forEach((a) => { const s = document.querySelector(a.getAttribute('href')); if (s) io.observe(s); });
}

main().catch((e) => {
  console.error(e);
  document.getElementById('site-title').textContent = `Couldn't load this site: ${e.message}`;
});
