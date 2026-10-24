import './styles.css';
import * as d3 from 'd3';
import maplibregl from 'maplibre-gl';
import { createMap } from './lib/map.js';
import { FEATURED, NO_DATA, PCT_CLASSES, ago, fmtFlow, fmtPct, fmtTemp, initUnitToggle, loadJSON, pctBadge, pctClass, renderFooter, renderNav, siteHref, units } from './lib/common.js';

initUnitToggle();
renderNav(null);
renderFooter();

const MODES = {
  pct: {
    color: ['case', ['==', ['get', 'pct'], -1], NO_DATA, ['step', ['get', 'pct'], PCT_CLASSES[0].color, 0.1, PCT_CLASSES[1].color, 0.25, PCT_CLASSES[2].color, 0.75, PCT_CLASSES[3].color, 0.9, PCT_CLASSES[4].color]],
    legend: () => [...PCT_CLASSES].reverse().map((c) => `<div class="legend-row"><span class="legend-swatch" style="background:${c.color}"></span>${c.label}</div>`).join('') + `<div class="legend-row"><span class="legend-swatch" style="background:${NO_DATA}"></span>No recent reading</div>`,
  },
  snow: {
    color: ['interpolate', ['linear'], ['get', 'snow'], 0, '#f2d8a7', 0.15, '#9cc3f0', 0.3, '#5b3cc4', 0.45, '#2a1a6e'],
    legend: () => `<div class="legend-ramp" style="background:linear-gradient(90deg,#f2d8a7,#9cc3f0,#5b3cc4,#2a1a6e)"></div><div class="legend-ends"><span>Little snow</span><span>45%+ of precipitation as snow</span></div>`,
  },
  rb: {
    color: ['interpolate', ['linear'], ['coalesce', ['get', 'rb'], 0], 0.05, '#cfe3d8', 0.3, '#5fb08a', 0.6, '#e8a14a', 1.0, '#c0392b'],
    legend: () => `<div class="legend-ramp" style="background:linear-gradient(90deg,#cfe3d8,#5fb08a,#e8a14a,#c0392b)"></div><div class="legend-ends"><span>Steady</span><span>Flashy (rises and falls fast)</span></div>`,
  },
};

const legendEl = document.getElementById('ov-legend');

async function main() {
  const data = await loadJSON('sites.json');
  document.getElementById('ov-asof').textContent = `${data.sites.filter((s) => s.pct != null).length} of ${data.sites.length} gauges reporting · USGS provisional data, ${ago(new Date(data.generated_at))}`;
  const fc = {
    type: 'FeatureCollection',
    features: data.sites.map((s) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] }, properties: { ...s, pct: s.pct ?? -1, featured: s.featured || '' } })),
  };

  const map = createMap('overview-map', { center: [-76.5, 40.5], zoom: 4.6, minZoom: 3 });
  map.on('load', () => {
    map.addSource('sites', { type: 'geojson', data: fc });
    map.addLayer({
      id: 'sites', type: 'circle', source: 'sites', filter: ['==', ['get', 'featured'], ''],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 2.6, 6, 4.5, 9, 7],
        'circle-color': MODES.pct.color, 'circle-stroke-color': '#fff', 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 3, 0.4, 7, 1.2], 'circle-opacity': 0.92,
      },
    });
    map.addLayer({ id: 'featured-halo', type: 'circle', source: 'sites', filter: ['!=', ['get', 'featured'], ''], paint: { 'circle-radius': 15, 'circle-color': '#0b2a3f', 'circle-opacity': 0.14 } });
    map.addLayer({
      id: 'featured', type: 'circle', source: 'sites', filter: ['!=', ['get', 'featured'], ''],
      paint: { 'circle-radius': 8, 'circle-color': MODES.pct.color, 'circle-stroke-color': '#0b2a3f', 'circle-stroke-width': 2.5 },
    });
    const short = Object.fromEntries(FEATURED.map((f) => [f.id, f.short]));
    map.addLayer({
      id: 'featured-label', type: 'symbol', source: 'sites', filter: ['!=', ['get', 'featured'], ''],
      layout: { 'text-field': ['match', ['get', 'id'], ...Object.entries(short).flat(), ''], 'text-font': ['Noto Sans Bold'], 'text-size': 13, 'text-offset': [0.9, 0], 'text-anchor': 'left', 'text-allow-overlap': true },
      paint: { 'text-color': '#0b2a3f', 'text-halo-color': '#fff', 'text-halo-width': 2 },
    });
    const bounds = new maplibregl.LngLatBounds();
    data.sites.forEach((s) => bounds.extend([s.lon, s.lat]));
    const mobile = window.innerWidth < 640;
    map.fitBounds(bounds, { padding: mobile ? 24 : { top: 40, bottom: 40, left: Math.min(440, window.innerWidth * 0.4), right: 40 }, duration: 0 });

    const popup = new maplibregl.Popup({ closeButton: false, offset: 10, maxWidth: '260px' });
    const html = (p) => {
      const c = pctClass(p.pct < 0 ? null : p.pct);
      const name = String(p.name).replace(/\b([A-Z]{2,})\b/g, (w) => (w.length <= 2 ? w : w[0] + w.slice(1).toLowerCase()));
      return `<h4>${name}</h4><p>USGS ${p.id} · ${Number(p.area_km2 / 2.59).toFixed(0)} mi²</p>
        <p><span style="color:${c.color}">●</span> ${c.label}${p.q && p.q !== 'null' ? ` · ${fmtFlow(+p.q)}` : ''}</p>
        <p>Snow share ${fmtPct(+p.snow)}${p.below_dam === true || p.below_dam === 'true' ? ' · below a dam' : ''}</p>
        ${p.featured ? `<p><a href="${siteHref(p.id)}">Open forecast page →</a></p>` : '<p class="muted" style="color:#6b7a87">Forecast page coming later</p>'}`;
    };
    for (const layer of ['sites', 'featured']) {
      map.on('mousemove', layer, (e) => { map.getCanvas().style.cursor = 'pointer'; popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map); });
      map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
    }
    map.on('click', 'featured', (e) => { location.href = siteHref(e.features[0].properties.id); });
    map.on('click', 'sites', (e) => popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map));

    const setMode = (mode) => {
      map.setPaintProperty('sites', 'circle-color', MODES[mode].color);
      map.setPaintProperty('featured', 'circle-color', MODES[mode].color);
      legendEl.innerHTML = MODES[mode].legend();
    };
    document.getElementById('color-mode').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      document.querySelectorAll('#color-mode button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
      setMode(b.dataset.mode);
    });
    setMode('pct');
  });

  renderCards(data);
}

async function renderCards(data) {
  const el = document.getElementById('featured-cards');
  const byId = Object.fromEntries(data.sites.map((s) => [s.id, s]));
  el.innerHTML = FEATURED.map((f) => `<a class="site-card" href="${siteHref(f.id)}" id="card-${f.id}"><div class="site-card-map"></div><div class="site-card-body"><p class="skeleton">Loading…</p></div></a>`).join('');
  await Promise.all(FEATURED.map(async (f) => {
    const [meta, geo, recent, skill] = await Promise.all([loadJSON(`sites/${f.id}/meta.json`), loadJSON(`sites/${f.id}/geo.json`), loadJSON(`sites/${f.id}/recent.json`), loadJSON(`sites/${f.id}/skill.json`)]);
    const card = document.getElementById(`card-${f.id}`);
    miniMap(card.querySelector('.site-card-map'), geo);
    const o = byId[f.id] || {};
    const flowV = recent.flow.v.filter((v) => v != null);
    const tempV = recent.temp.v.filter((v) => v != null);
    const q = o.q ?? flowV.at(-1);
    const t = tempV.at(-1);
    const s24 = skill.flow.rows.find((r) => r.lead_h === 24);
    const s72 = skill.flow.rows.find((r) => r.lead_h === 72);
    card.querySelector('.site-card-body').innerHTML = `
      <span class="kind">${meta.kind}</span>
      <h3>${meta.name}</h3>
      <div class="stats">
        <div class="stat-sm"><span class="v">${fmtFlow(q)}</span><span class="l">flow now</span></div>
        <div class="stat-sm"><span class="v" data-temp="${t ?? ''}"></span><span class="l">water temperature</span></div>
        <div class="stat-sm"><span class="v">${Math.round(meta.area_mi2).toLocaleString()} mi²</span><span class="l">drainage area</span></div>
      </div>
      <div>${pctBadge(o.pct)}</div>
      <p class="skill-line">Forecast error ${fmtPct(s24.skill)} smaller than persistence at 1 day, ${fmtPct(s72.skill)} at 3 days.</p>`;
    renderTemps();
  }));
}

function renderTemps() {
  document.querySelectorAll('[data-temp]').forEach((n) => {
    n.textContent = fmtTemp(n.dataset.temp === '' ? null : +n.dataset.temp, 0);
  });
}
units.onChange(renderTemps);

function miniMap(el, geo) {
  const w = 400;
  const h = 150;
  const proj = d3.geoMercator().fitExtent([[14, 12], [w - 14, h - 12]], geo.basin);
  const path = d3.geoPath(proj);
  const maxOrder = d3.max(geo.rivers.features, (f) => f.properties.order) || 2;
  const svg = d3.select(el).append('svg').attr('viewBox', `0 0 ${w} ${h}`).attr('preserveAspectRatio', 'xMidYMid slice');
  svg.append('rect').attr('width', w).attr('height', h).attr('fill', '#e9f0f6');
  svg.append('path').attr('d', path(geo.basin)).attr('fill', '#ffffff').attr('stroke', '#0b2a3f').attr('stroke-width', 1.2).attr('stroke-opacity', 0.6);
  svg.append('g').selectAll('path').data(geo.rivers.features.filter((f) => f.properties.order >= maxOrder - 3)).join('path')
    .attr('d', path).attr('fill', 'none').attr('stroke', '#1f7ae0').attr('stroke-linecap', 'round')
    .attr('stroke-width', (f) => 0.4 + (f.properties.order - (maxOrder - 3)) * 0.65).attr('stroke-opacity', 0.85);
  const [gx, gy] = proj(geo.gauge.geometry.coordinates);
  svg.append('circle').attr('cx', gx).attr('cy', gy).attr('r', 5).attr('fill', '#0b2a3f').attr('stroke', '#fff').attr('stroke-width', 2);
}

main().catch((e) => {
  console.error(e);
  document.getElementById('ov-asof').textContent = `Couldn't load data: ${e.message}`;
});
