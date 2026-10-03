import { useEffect, useMemo, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import { C } from '../lib/palette.js';
import { basinGrid, basinWeather, fmt } from '../lib/data.js';
import { basinMean, inPolygon, outerRings, renderField, sampleGrid } from '../lib/raster.js';

const BASEMAP = 'https://tiles.openfreemap.org/styles/positron';
const TERRAIN = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';

const LAYERS = [
  { key: 'rainNext', label: 'Rain, next 3 days', color: C.rain, show: (v) => fmt.in(v, 2), alpha: (v, max) => 0.85 * Math.min(1, v / Math.max(1, max)) },
  { key: 'rain24', label: 'Rain, past 24 h', color: C.rain, show: (v) => fmt.in(v, 2), alpha: (v, max) => 0.85 * Math.min(1, v / Math.max(0.5, max)) },
  { key: 'snowDepth', label: 'Snow on the ground', color: C.snow, show: (v) => fmt.in(v, 1), alpha: (v, max) => 0.85 * Math.min(1, v / Math.max(6, max)) },
  { key: 'sun', label: 'Sunshine today', color: C.sun, show: (v) => `${v.toFixed(1)} MJ/m²`, alpha: (v, max, min) => 0.15 + 0.6 * ((v - min) / Math.max(1, max - min)) },
  { key: 'airTemp', label: 'Air temperature now', color: C.alert, show: (v) => `${Math.round(v)}°F`, alpha: (v, max, min) => 0.1 + 0.6 * ((v - min) / Math.max(2, max - min)) },
];

export default function Basin({ meta, geo }) {
  const grid = useMemo(() => basinGrid(geo.bounds), [geo.bounds]);
  const [wx, setWx] = useState(null);
  const [wxError, setWxError] = useState(null);
  const [layer, setLayer] = useState('rainNext');
  useEffect(() => {
    const g = geo.basin.geometry;
    const s = grid.step;
    const near = ([x, y]) => [-s, 0, s].some((dx) => [-s, 0, s].some((dy) => inPolygon(g, [x + dx, y + dy])));
    basinWeather(grid, near).then(setWx, (e) => setWxError(String(e.message ?? e)));
  }, [grid, geo]);

  const means = useMemo(() => {
    if (!wx) return {};
    return Object.fromEntries(LAYERS.map((l) => [l.key, basinMean(grid, wx.fields[l.key], geo.basin.geometry)]));
  }, [wx, grid, geo]);

  const active = LAYERS.find((l) => l.key === layer);
  const facts = [
    ['Drainage area', `${fmt.int(meta.area_mi2)} mi²`],
    ['Mean elevation', `${fmt.ft(meta.elevation_m)} ft`],
    ['Forest', fmt.pct(meta.forest_frac)],
    ['Developed', fmt.pct(meta.developed_frac)],
    ['Precipitation', `${Math.round(meta.precip_mm_yr / 25.4)} in / yr`],
    ['Falls as snow', fmt.pct(meta.snow_frac)],
    ['Travel time to gauge', `~${Math.round(meta.travel_time_mean_h)} h (up to ${Math.round(meta.travel_time_max_h / 24)} days)`],
    ['Dams', `${meta.nid_dams} (${meta.nid_major_dams} major)`],
  ];

  return (
    <section className="card overflow-hidden">
      <div className="grid lg:grid-cols-[1fr_340px]">
        <BasinMap geo={geo} meta={meta} grid={grid} wx={wx} layer={active} />
        <div className="border-line p-5 sm:p-6 lg:border-l">
          <div className="eyebrow">The basin</div>
          <p className="mt-2 text-sm leading-relaxed text-ink/80">
            Everything upstream of the gauge. Water from the far headwaters takes up to {Math.round(meta.travel_time_max_h / 24)} days to arrive.
          </p>

          <div className="mt-5 flex items-baseline justify-between">
            <div className="eyebrow">Conditions now</div>
            <div className="text-[11px] text-faint">basin average</div>
          </div>
          <div className="mt-2 flex flex-col gap-1">
            {LAYERS.map((l) => (
              <button
                key={l.key}
                onClick={() => setLayer(l.key)}
                className={`flex items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition ${
                  l.key === layer ? 'bg-paper ring-1 ring-line' : 'hover:bg-paper/60'
                }`}
              >
                <span className="flex items-center gap-2.5">
                  <span className="size-3 rounded-full" style={{ background: l.color, opacity: l.key === layer ? 1 : 0.55 }} />
                  <span className={l.key === layer ? 'font-medium' : 'text-muted'}>{l.label}</span>
                </span>
                <span className="font-semibold tabular-nums">{means[l.key] == null ? (wxError ? '—' : '…') : l.show(means[l.key])}</span>
              </button>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-faint">
            {wxError ? `Live weather unavailable (${wxError}).` : `Live weather from Open-Meteo${wx?.time ? `, ${wx.time.replace('T', ' ')} ET` : ''}.`}
          </p>

          <dl className="mt-6 grid grid-cols-2 gap-x-5 gap-y-3">
            {facts.map(([k, v]) => (
              <div key={k}>
                <dt className="text-xs text-muted">{k}</dt>
                <dd className="text-sm font-medium tabular-nums">{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </section>
  );
}

function BasinMap({ geo, meta, grid, wx, layer }) {
  const el = useRef(null);
  const mapRef = useRef(null);
  const [ready, setReady] = useState(false);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    const [x0, y0, x1, y1] = geo.bounds;
    const map = new maplibregl.Map({
      container: el.current,
      style: BASEMAP,
      bounds: [
        [x0, y0],
        [x1, y1],
      ],
      fitBoundsOptions: { padding: 28 },
      attributionControl: { compact: true },
      cooperativeGestures: true,
      canvasContextAttributes: { preserveDrawingBuffer: true },
    });
    mapRef.current = map;
    if (import.meta.env.DEV) window.__map = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    const fit = () => {
      map.resize();
      map.fitBounds([[x0, y0], [x1, y1]], { padding: { top: 28, right: 28, left: 28, bottom: 44 }, duration: 0 });
    };
    let lastWidth = 0;
    const ro = new ResizeObserver(([e]) => {
      if (Math.abs(e.contentRect.width - lastWidth) < 1) return;
      lastWidth = e.contentRect.width;
      fit();
    });
    ro.observe(el.current);
    map.on('load', () => {
      fit();
      const firstSymbol = map.getStyle().layers.find((l) => l.type === 'symbol')?.id;
      map.addSource('dem', { type: 'raster-dem', tiles: [TERRAIN], encoding: 'terrarium', tileSize: 256, maxzoom: 12, attribution: 'Terrain: Mapzen / AWS Open Data' });
      map.addLayer(
        { id: 'hillshade', type: 'hillshade', source: 'dem', paint: { 'hillshade-exaggeration': 0.45, 'hillshade-shadow-color': '#4a4a42', 'hillshade-highlight-color': '#ffffff' } },
        firstSymbol,
      );

      const holes = outerRings(geo.basin.geometry);
      map.addSource('mask', {
        type: 'geojson',
        data: { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]], ...holes] } },
      });
      map.addLayer({ id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': C.paper, 'fill-opacity': 0.72 } }, firstSymbol);

      map.addSource('wx', { type: 'image', url: blankPng(), coordinates: [[0, 1], [1, 1], [1, 0], [0, 0]] });
      map.addLayer({ id: 'wx', type: 'raster', source: 'wx', paint: { 'raster-opacity': 0.9, 'raster-fade-duration': 0 } }, firstSymbol);

      map.addSource('basin', { type: 'geojson', data: geo.basin });
      map.addLayer({ id: 'basin-line', type: 'line', source: 'basin', paint: { 'line-color': C.ink, 'line-width': 1.6, 'line-opacity': 0.85 } });

      map.addSource('rivers', { type: 'geojson', data: geo.rivers });
      map.addLayer({
        id: 'rivers',
        type: 'line',
        source: 'rivers',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': C.flow,
          'line-opacity': ['interpolate', ['linear'], ['get', 'order'], 2, 0.6, 5, 1],
          'line-width': ['interpolate', ['linear'], ['zoom'], 7, ['interpolate', ['linear'], ['get', 'order'], 2, 0.7, 4, 1.4, 7, 3.4], 11, ['interpolate', ['linear'], ['get', 'order'], 2, 1.4, 4, 2.6, 7, 6]],
        },
      });

      const big = { ...geo.dams, features: geo.dams.features.filter((f) => f.properties.storage_af >= 50000) };
      map.addSource('dams', { type: 'geojson', data: big });
      map.addLayer({
        id: 'dams',
        type: 'circle',
        source: 'dams',
        paint: { 'circle-radius': 5, 'circle-color': C.ink, 'circle-stroke-color': '#fff', 'circle-stroke-width': 1.5 },
      });
      map.addLayer({
        id: 'dams-label',
        type: 'symbol',
        source: 'dams',
        layout: {
          'text-field': ['get', 'name'],
          'text-font': ['Noto Sans Regular'],
          'text-size': 11,
          'text-offset': [0, 0.9],
          'text-anchor': 'top',
        },
        paint: { 'text-color': C.muted, 'text-halo-color': '#fff', 'text-halo-width': 1.5 },
      });

      map.addSource('gauges', { type: 'geojson', data: geo.gauges });
      map.addLayer({
        id: 'gauges',
        type: 'circle',
        source: 'gauges',
        filter: ['==', ['get', 'active'], true],
        paint: { 'circle-radius': 3.5, 'circle-color': '#fff', 'circle-stroke-color': C.flow, 'circle-stroke-width': 1.5 },
      });

      map.addSource('site', { type: 'geojson', data: geo.gauge });
      map.addLayer({ id: 'site-halo', type: 'circle', source: 'site', paint: { 'circle-radius': 13, 'circle-color': C.flow, 'circle-opacity': 0.18 } });
      map.addLayer({ id: 'site', type: 'circle', source: 'site', paint: { 'circle-radius': 6.5, 'circle-color': C.flow, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
      map.addLayer({
        id: 'site-label',
        type: 'symbol',
        source: 'site',
        layout: { 'text-field': meta.short, 'text-font': ['Noto Sans Bold'], 'text-size': 13, 'text-offset': [0, 1.3], 'text-anchor': 'top' },
        paint: { 'text-color': C.ink, 'text-halo-color': '#fff', 'text-halo-width': 2 },
      });

      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: 'text-xs' });
      const show = (html) => (e) => {
        map.getCanvas().style.cursor = 'pointer';
        popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map);
      };
      const hide = () => {
        map.getCanvas().style.cursor = '';
        popup.remove();
      };
      map.on('mouseenter', 'gauges', show((p) => `<b>${p.name}</b><br>${p.q != null ? `${Number(p.q).toLocaleString()} cfs` : ''}`));
      map.on('mouseleave', 'gauges', hide);
      map.on('mouseenter', 'dams', show((p) => `<b>${p.name}</b><br>${p.river ?? ''} · built ${p.year}<br>${Math.round(p.storage_af).toLocaleString()} acre-ft`));
      map.on('mouseleave', 'dams', hide);
      setReady(true);
    });
    return () => {
      ro.disconnect();
      map.remove();
    };
  }, [geo, meta]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !wx || !map) return;
    const values = wx.fields[layer.key];
    const present = values.filter((v) => v != null);
    const max = Math.max(...present);
    const min = Math.min(...present);
    const { url, coordinates } = renderField(grid, values, geo.basin.geometry, layer.color, (v) => layer.alpha(v, max, min));
    map.getSource('wx').updateImage({ url, coordinates });
    const move = (e) => {
      const p = [e.lngLat.lng, e.lngLat.lat];
      const v = inPolygon(geo.basin.geometry, p) ? sampleGrid(grid, values, ...p) : null;
      setHover(v == null ? null : { x: e.point.x, y: e.point.y, v });
    };
    const leave = () => setHover(null);
    map.on('mousemove', move);
    map.on('mouseout', leave);
    return () => {
      map.off('mousemove', move);
      map.off('mouseout', leave);
    };
  }, [ready, wx, layer, grid, geo]);

  return (
    <div className="relative min-h-[420px] lg:min-h-[560px]">
      {/* Inline because maplibre-gl.css is unlayered and its position: relative beats Tailwind utilities. */}
      <div ref={el} style={{ position: 'absolute', inset: 0 }} />
      <div className="pointer-events-none absolute top-3 left-3 rounded-lg bg-white/90 px-3 py-2 text-xs shadow-sm ring-1 ring-line backdrop-blur">
        <div className="flex items-center gap-2 font-medium">
          <span className="size-2.5 rounded-full" style={{ background: layer.color }} />
          {layer.label}
        </div>
        <div className="mt-1.5 flex items-center gap-3 text-muted">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 rounded" style={{ background: C.flow }} />
            Rivers
          </span>
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-full border-[1.5px] bg-white" style={{ borderColor: C.flow }} />
            Gauges
          </span>
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-full" style={{ background: C.ink }} />
            Reservoirs
          </span>
        </div>
      </div>
      {hover && wx && (
        <div
          className="pointer-events-none absolute rounded bg-ink px-1.5 py-0.5 text-[11px] font-medium text-white tabular-nums"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
        >
          {layer.show(hover.v)}
        </div>
      )}
    </div>
  );
}

function blankPng() {
  const c = document.createElement('canvas');
  c.width = c.height = 1;
  return c.toDataURL();
}
