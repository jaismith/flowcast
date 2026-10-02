import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

export const BASEMAP = 'https://tiles.openfreemap.org/styles/positron';
const TERRAIN_TILES = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';
const FONT = ['Noto Sans Regular'];
const FONT_BOLD = ['Noto Sans Bold'];

export const ROLE_COLORS = { below_dam: '#7c3aed', model_input: '#0f9d8a', upstream: '#1f7ae0' };
export const ROLE_LABELS = { below_dam: 'Below a reservoir (model input)', model_input: 'Upstream (model input)', upstream: 'Other upstream gauge' };

export function createMap(container, opts = {}) {
  const map = new maplibregl.Map({
    container,
    style: BASEMAP,
    attributionControl: { compact: true },
    cooperativeGestures: window.matchMedia('(pointer: coarse)').matches,
    dragRotate: false,
    pitchWithRotate: false,
    ...opts,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  return map;
}

export function firstSymbolLayer(map) {
  return map.getStyle().layers.find((l) => l.type === 'symbol')?.id;
}

export function addHillshade(map, before) {
  map.addSource('terrain-dem', { type: 'raster-dem', tiles: [TERRAIN_TILES], encoding: 'terrarium', tileSize: 256, maxzoom: 13, attribution: 'Terrain: <a href="https://registry.opendata.aws/terrain-tiles/">AWS Terrain Tiles</a>' });
  map.addLayer({
    id: 'hillshade', type: 'hillshade', source: 'terrain-dem',
    paint: { 'hillshade-exaggeration': 0.35, 'hillshade-shadow-color': '#5b6b78', 'hillshade-highlight-color': '#ffffff', 'hillshade-accent-color': '#8a9aa6' },
  }, before);
}

function outerRings(geom) {
  return geom.type === 'Polygon' ? [geom.coordinates[0]] : geom.coordinates.map((p) => p[0]);
}

function maskFeature(geom) {
  const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
  return { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [world, ...outerRings(geom).map((r) => [...r].reverse())] } };
}

export function addBasinLayers(map, geo, { siteName }) {
  const before = firstSymbolLayer(map);
  addHillshade(map, before);
  map.addSource('mask', { type: 'geojson', data: maskFeature(geo.basin.geometry) });
  map.addLayer({ id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': '#f6f8fa', 'fill-opacity': 0.62 } }, before);
  map.addSource('basin', { type: 'geojson', data: geo.basin });
  map.addLayer({ id: 'basin-fill', type: 'fill', source: 'basin', paint: { 'fill-color': '#1f7ae0', 'fill-opacity': 0.04 } }, before);
  map.addLayer({ id: 'basin-line', type: 'line', source: 'basin', paint: { 'line-color': '#0b2a3f', 'line-width': 2.2, 'line-opacity': 0.85 } }, before);

  map.addSource('rivers', { type: 'geojson', data: geo.rivers });
  const maxOrder = Math.max(...geo.rivers.features.map((f) => f.properties.order), 2);
  map.addLayer({
    id: 'rivers', type: 'line', source: 'rivers', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': ['interpolate', ['linear'], ['get', 'order'], 1, '#7fb6ef', maxOrder, '#1462c4'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 7, ['interpolate', ['linear'], ['get', 'order'], 1, 0.5, maxOrder, 3], 12, ['interpolate', ['linear'], ['get', 'order'], 1, 1.4, maxOrder, 7]],
    },
  }, before);
  map.addLayer({
    id: 'river-labels', type: 'symbol', source: 'rivers', filter: ['all', ['has', 'name'], ['>=', ['get', 'order'], Math.max(maxOrder - 3, 1)]],
    layout: { 'symbol-placement': 'line', 'text-field': ['get', 'name'], 'text-font': FONT, 'text-size': 11, 'symbol-spacing': 280, 'text-max-angle': 30 },
    paint: { 'text-color': '#1462c4', 'text-halo-color': 'rgba(255,255,255,0.9)', 'text-halo-width': 1.4 },
  });

  map.addSource('dams', { type: 'geojson', data: geo.dams });
  map.addLayer({
    id: 'dams', type: 'circle', source: 'dams',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['sqrt', ['max', ['get', 'storage_af'], 1]], 1, 2.2, 30, 4, 300, 8, 800, 13],
      'circle-color': '#6b4f3a', 'circle-opacity': 0.85, 'circle-stroke-color': '#fff', 'circle-stroke-width': 1,
    },
  });
  map.addLayer({
    id: 'dam-labels', type: 'symbol', source: 'dams', filter: ['>=', ['get', 'storage_af'], 20000],
    layout: { 'text-field': ['get', 'name'], 'text-font': FONT, 'text-size': 11, 'text-offset': [0, 1.2], 'text-anchor': 'top', 'text-optional': true },
    paint: { 'text-color': '#4a3626', 'text-halo-color': '#fff', 'text-halo-width': 1.4 },
  });

  map.addSource('gauges', { type: 'geojson', data: geo.gauges });
  map.addLayer({
    id: 'gauges', type: 'circle', source: 'gauges',
    paint: {
      'circle-radius': ['case', ['==', ['get', 'role'], 'upstream'], 4.5, 6],
      'circle-color': ['match', ['get', 'role'], 'below_dam', ROLE_COLORS.below_dam, 'model_input', ROLE_COLORS.model_input, '#ffffff'],
      'circle-stroke-color': ['match', ['get', 'role'], 'upstream', ROLE_COLORS.upstream, '#ffffff'],
      'circle-stroke-width': ['match', ['get', 'role'], 'upstream', 2, 1.5],
    },
  });

  map.addSource('site', { type: 'geojson', data: geo.gauge });
  map.addLayer({ id: 'site-halo', type: 'circle', source: 'site', paint: { 'circle-radius': 16, 'circle-color': '#1f7ae0', 'circle-opacity': 0.18 } });
  map.addLayer({ id: 'site-dot', type: 'circle', source: 'site', paint: { 'circle-radius': 7.5, 'circle-color': '#0b2a3f', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2.5 } });
  map.addLayer({
    id: 'site-label', type: 'symbol', source: 'site',
    layout: { 'text-field': siteName, 'text-font': FONT_BOLD, 'text-size': 13, 'text-offset': [0, -1.6], 'text-anchor': 'bottom' },
    paint: { 'text-color': '#0b2a3f', 'text-halo-color': '#fff', 'text-halo-width': 2 },
  });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, maxWidth: '260px' });
  const hover = (layer, html) => {
    map.on('mousemove', layer, (e) => {
      map.getCanvas().style.cursor = 'pointer';
      popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map);
    });
    map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; popup.remove(); });
    map.on('click', layer, (e) => popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map));
  };
  hover('gauges', (p) => `<h4>${p.name}</h4><p>USGS ${p.id} · ${ROLE_LABELS[p.role]}</p>${p.q != null && p.q !== 'null' ? `<p>Latest flow <b>${Number(p.q).toLocaleString()} ft³/s</b></p>` : ''}`);
  hover('dams', (p) => `<h4>${p.name || 'Dam'}</h4><p>${p.river ? `${p.river} · ` : ''}${p.purpose || ''}${p.year && p.year !== 'null' ? ` · built ${p.year}` : ''}</p><p>Storage ${Number(p.storage_af).toLocaleString()} acre-ft</p>`);
  hover('site-dot', () => `<h4>${siteName}</h4><p>The forecast point (USGS ${geo.gauge.properties.id})</p>`);

  const [x0, y0, x1, y1] = geo.bounds;
  map.fitBounds([[x0, y0], [x1, y1]], { padding: { top: 60, bottom: 40, left: 30, right: 50 }, duration: 0 });
}

export const LAYER_GROUPS = {
  rivers: ['rivers', 'river-labels'],
  gauges: ['gauges'],
  dams: ['dams', 'dam-labels'],
  terrain: ['hillshade'],
};

export function setLayerGroup(map, group, visible) {
  for (const id of LAYER_GROUPS[group] || []) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
  }
}

export const RAIN_STOPS = [[0, '#f1f6fb'], [10, '#bcd7f3'], [25, '#6fa8e8'], [50, '#2f6fcf'], [80, '#5b3cc4'], [120, '#a3218f']];

export function setBasinRain(map, totalMm) {
  if (!map.getLayer('basin-fill')) return;
  if (totalMm == null) {
    map.setPaintProperty('basin-fill', 'fill-color', '#1f7ae0');
    map.setPaintProperty('basin-fill', 'fill-opacity', 0.04);
    return;
  }
  const expr = ['interpolate', ['linear'], ['literal', totalMm], ...RAIN_STOPS.flat()];
  map.setPaintProperty('basin-fill', 'fill-color', expr);
  map.setPaintProperty('basin-fill', 'fill-opacity', 0.55);
}
