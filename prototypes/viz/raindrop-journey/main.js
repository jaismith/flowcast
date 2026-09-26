import * as maplibregl from 'maplibre-gl';
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';
import * as d3 from 'd3';
import { loadJSON, mountTopbar, mountSource, fmtCfs, HOUR, clamp } from '../shared/common.js';

mountTopbar("A Raindrop's Journey", 'Scrollytelling · MapLibre 3D terrain');
mountSource('Path: USGS NLDI downstream-mainstem navigation over NHDPlus V2. Velocities and mean flows: NHDPlus EROM. Storm flows: USGS NWIS (provisional). Rain: Open-Meteo ERA5. Imagery: Esri World Imagery. Terrain: AWS Terrain Tiles. Reservoir capacity: NYC DEP.');

const [route, basin, rivers, waterbodies, storm, grid] = await Promise.all([
  loadJSON('raindrop-path.json'),
  loadJSON('basin.json'),
  loadJSON('rivers.json'),
  loadJSON('waterbodies.json'),
  loadJSON('gauges-storm.json'),
  loadJSON('weather-grid-daily.json'),
]);

// ---------- path geometry ----------
const hav = ([x1, y1], [x2, y2]) => {
  const R = 6371;
  const dLat = ((y2 - y1) * Math.PI) / 180;
  const dLon = ((x2 - x1) * Math.PI) / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos((y1 * Math.PI) / 180) * Math.cos((y2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
};
const raw = route.path.geometry.coordinates;
// The drop lands on the hillslope and joins the channel at the nearest vertex.
let joinIdx = 0;
raw.forEach((c, k) => { if (hav(c, route.start) < hav(raw[joinIdx], route.start)) joinIdx = k; });
const coords = [route.start, ...raw.slice(joinIdx)];
const cum = [0];
for (let k = 1; k < coords.length; k++) cum.push(cum[k - 1] + hav(coords[k - 1], coords[k]));
const totalKm = cum[cum.length - 1];

function pointAt(km) {
  km = clamp(km, 0, totalKm);
  const k = clamp(d3.bisectRight(cum, km), 1, cum.length - 1);
  const t = (km - cum[k - 1]) / (cum[k] - cum[k - 1] || 1);
  const a = coords[k - 1];
  const b = coords[k];
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}
const kmNear = (ll) => { let best = 0; coords.forEach((c, k) => { if (hav(c, ll) < hav(coords[best], ll)) best = k; }); return cum[best]; };
function bearingAt(km) {
  const a = pointAt(km - 1.5);
  const b = pointAt(km + 4);
  return (Math.atan2((b[0] - a[0]) * Math.cos((a[1] * Math.PI) / 180), b[1] - a[1]) * 180) / Math.PI;
}

// Elevation profile from NHDPlus smoothed max elevations, laid along the route.
const segElev = [];
{
  let acc = cum[1];
  for (const s of route.segments) { segElev.push([acc, s.elevM]); acc += s.lengthKm; }
  segElev.push([totalKm, route.segments.at(-1).elevM - 3]);
}

// ---------- storm facts from the data ----------
const site = (id) => storm.sites.find((s) => s.id === id);
const stormT0 = Date.parse(storm.start);
const peak = (id) => {
  const s = site(id);
  if (!s) return null;
  let bi = 0;
  s.q.forEach((v, i) => { if (v != null && v > (s.q[bi] ?? -1)) bi = i; });
  return { q: s.q[bi], t: new Date(stormT0 + bi * HOUR), name: s.name, area: s.drainageSqMi, ll: [s.lon, s.lat] };
};
const P = {
  hobart: peak('01421610'),
  walton: peak('01423000'),
  stiles: peak('01425000'),
  haleEddy: peak('01426500'),
  hancockEB: peak('01421500'),
  beaverkill: peak('01420500'),
  lordville: peak('01427207'),
  callicoon: peak('01427510'),
};
const cannonsville = waterbodies.features.find((f) => /cannonsville/i.test(f.properties.name ?? ''));
// Travel time along channels at mean-annual velocity, excluding the reservoir pool.
const resKm = [];
if (cannonsville) coords.forEach((c, k) => { if (k && d3.geoContains(cannonsville, c)) resKm.push(cum[k]); });
const resRange = resKm.length ? [d3.min(resKm), d3.max(resKm)] : [0, 0];
let travelH = 0;
{
  let acc = cum[1];
  for (const s of route.segments) {
    const mid = acc + s.lengthKm / 2;
    if (!(mid >= resRange[0] && mid <= resRange[1]) && s.velocityFps) travelH += (s.lengthKm * 1000) / (s.velocityFps * 0.3048) / 3600;
    acc += s.lengthKm;
  }
}
// Rain on storm day at the grid point nearest the start
const nearestPt = d3.least(grid.points.map((p, k) => [k, hav(p, route.start)]), (d) => d[1])[0];
const stormDay = Math.round((P.callicoon.t - Date.parse(grid.start)) / 864e5) - 1;
const rainStart = d3.sum(d3.range(stormDay - 2, stormDay + 1), (d) => grid.precip[nearestPt][d] ?? 0);
const fmtDay = d3.utcFormat('%b %-d');

const KM = {
  start: 0,
  hobart: kmNear(P.hobart.ll),
  walton: kmNear(P.walton.ll),
  resIn: resRange[0] || kmNear([-75.2, 42.1]),
  dam: kmNear(P.stiles.ll),
  hancock: kmNear([-75.2877, 41.9535]),
  lordville: kmNear(P.lordville.ll),
  end: totalKm,
};

// ---------- story ----------
const STEPS = [
  {
    km: [0, 0], zoom: 8.3, pitch: 35, bearing: -10, center: d3.geoCentroid(basin), where: 'Upper Delaware basin', rain: 0,
    html: `<div class="kicker">flowcast · visualization prototype</div><h2>A raindrop's journey</h2>
      <p>Where does the water at the Callicoon gauge come from? Follow one drop from a Catskills hillside to the gauge, ${totalKm.toFixed(0)} km downstream, during the storm that peaked on ${fmtDay(P.callicoon.t)}, 2026.</p>
      <p style="color:var(--muted)">Scroll to begin ↓</p>`, hero: true,
  },
  {
    km: [0, 0], zoom: 13.6, pitch: 72, bearing: 200, where: 'Hillslope above Stamford, NY', rain: 1,
    html: `<div class="kicker">Hillslope · ${route.segments[0].elevM.toFixed(0)}+ m</div><h2>It lands on a Catskills slope</h2>
      <p>On a ridge above Stamford, NY, near the source of the West Branch Delaware. Over the three days before the peak, <b>${rainStart.toFixed(2)} in</b> of rain fell on this grid cell.</p>
      <p>Most of it soaks into thin, rocky soil. Some runs off over the surface, and some reaches the stream days later as groundwater. A basin-scale model needs to keep track of all three paths.</p>`,
  },
  {
    km: [0, KM.hobart], zoom: 12.4, pitch: 65, where: 'West Branch headwaters', rain: 0.6,
    html: `<div class="kicker">First gauge · USGS 01421610</div><h2>Into the West Branch</h2>
      <p>A few kilometres down, the drop reaches the channel. At Hobart the river drains just <b>${P.hobart.area} sq mi</b>, and during this storm it peaked at <b>${fmtCfs(P.hobart.q)}</b>.</p>`,
  },
  {
    km: [KM.hobart, KM.walton], zoom: 11.2, pitch: 60, where: 'Delhi → Walton', rain: 0.2,
    html: `<div class="kicker">Gathering tributaries</div><h2>The valley fills in</h2>
      <p>The Little Delaware and dozens of hollows join in. By Walton the West Branch drains <b>${P.walton.area} sq mi</b> and peaked at <b>${fmtCfs(P.walton.q)}</b> on ${fmtDay(P.walton.t)}.</p>`,
  },
  {
    km: [KM.walton, KM.dam], zoom: 11, pitch: 55, where: 'Cannonsville Reservoir', rain: 0,
    html: `<div class="kicker">NYC water supply</div><h2>Then it stops: Cannonsville Reservoir</h2>
      <p>The drop enters a 95.7-billion-gallon reservoir that supplies New York City. It could stay here for months.</p>
      <div class="stat"><div><div class="label">Walton (inflow)</div><div class="big">${fmtCfs(P.walton.q)}</div></div><div><div class="label">Below the dam</div><div class="big">${fmtCfs(P.stiles.q)}</div></div></div>
      <p style="margin-top:10px">The reservoir absorbed the flood. Releases from it are the largest input missing from today's flowcast model.</p>`,
  },
  {
    km: [KM.dam, KM.hancock], zoom: 11, pitch: 60, where: 'Hale Eddy → Hancock', rain: 0,
    html: `<div class="kicker">Confluence at Hancock</div><h2>The Delaware River is born</h2>
      <p>At Hancock the West Branch (${fmtCfs(P.haleEddy.q)} at Hale Eddy) meets the East Branch, which was carrying <b>${fmtCfs(P.hancockEB.q)}</b>, mostly from the Beaver Kill (peak ${fmtCfs(P.beaverkill.q)}). That storm mostly missed the West Branch.</p>`,
  },
  {
    km: [KM.hancock, KM.end], zoom: 11.3, pitch: 62, where: 'Delaware River → Callicoon', rain: 0,
    html: `<div class="kicker">USGS 01427510 · the flowcast gauge</div><h2>Callicoon, ${totalKm.toFixed(0)} km later</h2>
      <p>The gauge that flowcast forecasts peaked at <b>${fmtCfs(P.callicoon.q)}</b> on ${fmtDay(P.callicoon.t)}, draining <b>1,820 sq mi</b>.</p>
      <div class="stat"><div><div class="label">Channel travel</div><div class="big">~${(travelH / 24).toFixed(1)} days</div></div><div><div class="label">Plus reservoir</div><div class="big">weeks–months</div></div></div>
      <p style="margin-top:10px;color:var(--muted)">Channel time uses NHDPlus mean-annual velocities. Flood waves move faster than that.</p>
      <p><a href="../river-pulse/">See the whole network pulse →</a></p>`,
  },
];

const story = d3.select('#story');
story.selectAll('.step').data(STEPS).join('section').attr('class', 'step').html((s) => `<div class="card ${s.hero ? 'hero' : ''}">${s.html}</div>`);

// ---------- map ----------
maplibregl.setWorkerUrl(maplibreWorkerUrl);
const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      imagery: { type: 'raster', tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 18, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' },
      dem: { type: 'raster-dem', tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'], encoding: 'terrarium', tileSize: 256, maxzoom: 14, attribution: 'Terrain: Mapzen / AWS Terrain Tiles' },
      hillshadeDem: { type: 'raster-dem', tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'], encoding: 'terrarium', tileSize: 256, maxzoom: 14 },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#05080f' } },
      { id: 'imagery', type: 'raster', source: 'imagery', paint: { 'raster-brightness-max': 0.78, 'raster-saturation': -0.25, 'raster-contrast': 0.1 } },
      { id: 'hillshade', type: 'hillshade', source: 'hillshadeDem', paint: { 'hillshade-exaggeration': 0.35, 'hillshade-shadow-color': '#02040a', 'hillshade-highlight-color': '#ffffff' } },
    ],
    terrain: { source: 'dem', exaggeration: 1.6 },
    sky: { 'sky-color': '#0b1630', 'horizon-color': '#33507a', 'fog-color': '#0b1630', 'sky-horizon-blend': 0.6, 'horizon-fog-blend': 0.6, 'fog-ground-blend': 0.2 },
  },
  center: STEPS[0].center,
  zoom: STEPS[0].zoom,
  pitch: STEPS[0].pitch,
  bearing: STEPS[0].bearing,
  interactive: false,
  attributionControl: { compact: true },
  canvasContextAttributes: { preserveDrawingBuffer: true },
});

const trail = { type: 'Feature', geometry: { type: 'LineString', coordinates: [coords[0], coords[0]] }, properties: {} };
map.on('load', () => {
  map.addSource('basin', { type: 'geojson', data: basin });
  map.addSource('rivers', { type: 'geojson', data: rivers });
  map.addSource('route', { type: 'geojson', data: { type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: {} } });
  map.addSource('trail', { type: 'geojson', data: trail, lineMetrics: true });
  map.addLayer({ id: 'basin-line', type: 'line', source: 'basin', paint: { 'line-color': '#80ffdb', 'line-width': 2, 'line-dasharray': [2, 2], 'line-opacity': 0.8 } });
  map.addLayer({ id: 'rivers', type: 'line', source: 'rivers', paint: { 'line-color': '#4cc9f0', 'line-opacity': 0.55, 'line-width': ['interpolate', ['linear'], ['get', 'order'], 2, 0.6, 5, 3] } });
  map.addLayer({ id: 'route', type: 'line', source: 'route', paint: { 'line-color': '#ffffff', 'line-opacity': 0.35, 'line-width': 2, 'line-dasharray': [1, 2] } });
  map.addLayer({ id: 'trail-glow', type: 'line', source: 'trail', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#80ffdb', 'line-width': 12, 'line-blur': 8, 'line-opacity': 0.6 } });
  map.addLayer({ id: 'trail', type: 'line', source: 'trail', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-width': 4, 'line-gradient': ['interpolate', ['linear'], ['line-progress'], 0, '#4361ee', 0.5, '#4cc9f0', 1, '#ffffff'] } });
  update(true);
});

const dropEl = document.createElement('div');
dropEl.className = 'drop';
const dropMarker = new maplibregl.Marker({ element: dropEl }).setLngLat(coords[0]).addTo(map);
for (const [key, label] of [['hobart', 'Hobart'], ['walton', 'Walton'], ['stiles', 'Below Cannonsville dam'], ['hancockEB', 'East Branch at Hancock'], ['callicoon', 'Callicoon gauge']]) {
  const el = document.createElement('div');
  el.className = 'gauge-marker';
  el.textContent = `${label} · ${d3.format('.3~s')(P[key].q)} cfs peak`;
  new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -6] }).setLngLat(P[key].ll).addTo(map);
}

// ---------- profile ----------
const prof = d3.select('#profile svg');
let profX;
function drawProfile() {
  const node = prof.node();
  const w = node.clientWidth;
  const h = node.clientHeight;
  const m = { l: 40, r: 10, t: 16, b: 20 };
  profX = d3.scaleLinear([0, totalKm], [m.l, w - m.r]);
  const y = d3.scaleLinear([150, d3.max(segElev, (d) => d[1]) + 60], [h - m.b, m.t]);
  const pts = [[0, route.segments[0].elevM + 90], ...segElev];
  prof.selectAll('*').remove();
  prof.append('rect').attr('x', profX(resRange[0])).attr('width', Math.max(2, profX(resRange[1]) - profX(resRange[0]))).attr('y', m.t).attr('height', h - m.b - m.t).attr('fill', 'rgba(31,111,176,0.35)');
  prof.append('text').attr('x', profX((resRange[0] + resRange[1]) / 2)).attr('y', m.t + 10).attr('text-anchor', 'middle').style('font-size', '10px').text('Cannonsville');
  prof.append('path').attr('d', d3.area().x((d) => profX(d[0])).y0(h - m.b).y1((d) => y(d[1])).curve(d3.curveMonotoneX)(pts)).attr('fill', 'rgba(128,255,219,0.12)').attr('stroke', 'rgba(128,255,219,0.6)');
  prof.append('g').attr('class', 'axis').attr('transform', `translate(0,${h - m.b})`).call(d3.axisBottom(profX).ticks(4).tickFormat((d) => `${d} km`));
  prof.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(3).tickFormat((d) => `${d} m`));
  prof.append('text').attr('x', m.l + 4).attr('y', m.t - 4).style('font-size', '10.5px').text('Channel elevation (NHDPlus)');
  prof.append('circle').attr('class', 'me').attr('r', 5).attr('fill', '#fff').attr('stroke', '#4cc9f0').attr('stroke-width', 2);
  prof.node().__y = y;
  prof.node().__pts = pts;
}
drawProfile();
const elevAt = (km) => {
  const pts = prof.node().__pts;
  const k = clamp(d3.bisector((d) => d[0]).right(pts, km), 1, pts.length - 1);
  const [x0, e0] = pts[k - 1];
  const [x1, e1] = pts[k];
  return e0 + ((e1 - e0) * (km - x0)) / (x1 - x0 || 1);
};

// ---------- rain overlay ----------
const rainCanvas = document.getElementById('rain');
const rctx = rainCanvas.getContext('2d');
const streaks = d3.range(500).map(() => ({ x: Math.random(), y: Math.random(), v: 0.6 + Math.random() * 0.8, l: 10 + Math.random() * 16 }));
let rainLevel = 0;
let rainTarget = 0;
function resizeRain() { rainCanvas.width = innerWidth; rainCanvas.height = innerHeight; }
resizeRain();
function drawRain(dt) {
  rainLevel += (rainTarget - rainLevel) * Math.min(1, dt * 2);
  rctx.clearRect(0, 0, rainCanvas.width, rainCanvas.height);
  const n = Math.round(streaks.length * rainLevel);
  if (!n) return;
  rctx.strokeStyle = 'rgba(190,210,255,0.45)';
  rctx.lineWidth = 1;
  rctx.beginPath();
  for (let k = 0; k < n; k++) {
    const s = streaks[k];
    s.y += s.v * dt * 1.4;
    if (s.y > 1) { s.y = -0.05; s.x = Math.random(); }
    const x = s.x * rainCanvas.width;
    const y = s.y * rainCanvas.height;
    rctx.moveTo(x, y);
    rctx.lineTo(x - s.l * 0.18, y + s.l);
  }
  rctx.stroke();
}

// ---------- scroll → camera ----------
const lerp = (a, b, t) => a + (b - a) * t;
const ease = d3.easeCubicInOut;
function lerpAngle(a, b, t) { const d = ((((b - a) % 360) + 540) % 360) - 180; return a + d * t; }
let smoothBearing = null;
let targetBearing = null;
let lastTrailKm = -1;

function scrollState() {
  const sections = [...document.querySelectorAll('.step')];
  const mid = innerHeight * 0.55;
  let idx = 0;
  let prog = 0;
  sections.forEach((el, k) => {
    const r = el.getBoundingClientRect();
    if (r.top <= mid) { idx = k; prog = clamp((mid - r.top) / r.height, 0, 1); }
  });
  sections.forEach((el, k) => el.classList.toggle('active', k === idx));
  return { idx, prog };
}

function update(force = false) {
  const { idx, prog } = scrollState();
  const s = STEPS[idx];
  const prev = STEPS[Math.max(0, idx - 1)];
  const km = lerp(s.km[0], s.km[1], ease(prog));
  const here = pointAt(km);
  // Blend camera from the previous step's framing over the first 30% of the step.
  const tIn = ease(clamp(prog / 0.3, 0, 1));
  const zoom = lerp(prev.zoom, s.zoom, idx === 0 ? 1 : tIn);
  const pitch = lerp(prev.pitch, s.pitch, idx === 0 ? 1 : tIn);
  const bearing = s.bearing ?? bearingAt(km);
  targetBearing = bearing;
  smoothBearing = smoothBearing == null || force ? bearing : lerpAngle(smoothBearing, bearing, 0.12);
  const center = idx === 0 ? s.center : idx === 1 ? [lerp(STEPS[0].center[0], here[0], tIn), lerp(STEPS[0].center[1], here[1], tIn)] : here;
  map.jumpTo({ center, zoom, pitch, bearing: smoothBearing });
  dropMarker.setLngLat(here);
  dropEl.style.opacity = idx === 0 ? 0 : 1;
  if (map.getSource('trail') && (force || Math.abs(km - lastTrailKm) > 0.05)) {
    lastTrailKm = km;
    const k = d3.bisectRight(cum, km);
    trail.geometry.coordinates = [...coords.slice(0, Math.max(1, k)), here];
    if (trail.geometry.coordinates.length < 2) trail.geometry.coordinates.push(here);
    map.getSource('trail').setData(trail);
  }
  document.getElementById('dist').textContent = `${km.toFixed(1)} km`;
  document.getElementById('where').textContent = s.where;
  const y = prof.node().__y;
  prof.select('.me').attr('cx', profX(km)).attr('cy', y(elevAt(km)));
  rainTarget = s.rain;
}

let ticking = false;
addEventListener('scroll', () => {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => { update(); ticking = false; });
});
addEventListener('resize', () => { resizeRain(); drawProfile(); update(true); });
let lastT = performance.now();
function loop(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000);
  lastT = now;
  drawRain(dt);
  // Keep easing the camera bearing toward the path direction until it settles.
  if (targetBearing != null && Math.abs(((((targetBearing - smoothBearing) % 360) + 540) % 360) - 180) > 0.3) update();
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
