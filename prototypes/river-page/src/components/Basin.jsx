import { useEffect, useMemo, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import { C } from '../lib/palette.js';
import { basinGrid, basinWeather, fmt } from '../lib/data.js';
import { basemapFor } from '../lib/basemaps.js';
import { basinMean, hexRgb, inPolygon, outerRings, ramp, renderField, sampleGrid } from '../lib/raster.js';

const TERRAIN = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';

// Color is stretched across the basin's own range so spatial pattern shows; opacity encodes absolute amount,
// so a dry day stays clear instead of being stretched into a full ramp of trace values.
const LAYERS = [
  { key: 'rainNext', label: 'Rain, next 3 days', color: C.rain, ramp: ['#bcd3fb', '#123f9c'], show: (v) => fmt.in(v, 2), alpha: (v) => 0.85 * Math.min(1, v / 0.3) },
  { key: 'rain24', label: 'Rain, past 24 h', color: C.rain, ramp: ['#bcd3fb', '#123f9c'], show: (v) => fmt.in(v, 2), alpha: (v) => 0.85 * Math.min(1, v / 0.2) },
  { key: 'snowDepth', label: 'Snow on the ground', color: C.snow, ramp: ['#e4dcfd', '#4b2fb8'], show: (v) => fmt.in(v, 1), alpha: (v) => 0.85 * Math.min(1, v / 1.5) },
  { key: 'sun', label: 'Sunshine today', color: C.sun, ramp: ['#fbe7a6', '#c96f00'], show: (v) => `${v.toFixed(1)} MJ/m²`, alpha: () => 0.7 },
  { key: 'airTemp', label: 'Air temperature now', color: C.alert, ramp: ['#9cc3ea', '#e0601c'], show: (v) => `${Math.round(v)}°F`, alpha: () => 0.65 },
];

export default function Basin({ meta, geo, at, initialLayer, variant = 'card', basemap: basemapName, viz = 'fill' }) {
  const basemap = basemapFor(basemapName, C.basemap);
  const grid = useMemo(() => basinGrid(geo.bounds), [geo.bounds]);
  const [wx, setWx] = useState(null);
  const [wxError, setWxError] = useState(null);
  const [layer, setLayer] = useState(initialLayer ?? 'rainNext');
  useEffect(() => {
    if (initialLayer) setLayer(initialLayer);
  }, [initialLayer, at]);
  useEffect(() => {
    const g = geo.basin.geometry;
    const s = grid.step;
    const near = ([x, y]) => [-s, 0, s].some((dx) => [-s, 0, s].some((dy) => inPolygon(g, [x + dx, y + dy])));
    let stale = false;
    setWx(null);
    setWxError(null);
    basinWeather(grid, near, at).then(
      (w) => !stale && setWx(w),
      (e) => !stale && setWxError(String(e.message ?? e)),
    );
    return () => {
      stale = true;
    };
  }, [grid, geo, at]);

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
  const note = wxError
    ? `Weather unavailable (${wxError}).`
    : `Live weather from Open-Meteo${at ? `, ${fmt.when(at)}` : wx?.time ? `, ${wx.time.replace('T', ' ')} ET` : ''}. Basin averages.`;

  if (variant === 'editorial') {
    return (
      <div>
        <div className="grid grid-cols-2 gap-x-6 sm:grid-cols-5">
          {LAYERS.map((l) => (
            <button
              key={l.key}
              onClick={() => setLayer(l.key)}
              className="border-t-2 pt-2 pb-3 text-left transition"
              style={{ borderColor: l.key === layer ? l.color : C.line }}
            >
              <div className={`text-[13px] ${l.key === layer ? 'text-ink' : 'text-muted'}`}>{l.label}</div>
              <div className="mt-0.5 text-lg font-semibold tabular-nums">{means[l.key] == null ? (wxError ? '—' : '…') : l.show(means[l.key])}</div>
            </button>
          ))}
        </div>
        <BasinMap geo={geo} meta={meta} grid={grid} wx={wx} layer={active} basemap={basemap} viz={viz} />
        <p className="mt-2 text-xs text-faint">{note}</p>
      </div>
    );
  }

  return (
    <section className="card overflow-hidden">
      <div className="grid lg:grid-cols-[1fr_340px]">
        <BasinMap geo={geo} meta={meta} grid={grid} wx={wx} layer={active} basemap={basemap} viz={viz} />
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
          <p className="mt-2 text-[11px] text-faint">{note}</p>

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

function BasinMap({ geo, meta, grid, wx, layer, basemap, viz }) {
  const el = useRef(null);
  const mapRef = useRef(null);
  const [ready, setReady] = useState(false);
  const [hover, setHover] = useState(null);
  const range = useMemo(() => wx && fieldRange(grid, wx.fields[layer.key], geo.basin.geometry), [wx, grid, layer, geo]);

  useEffect(() => {
    setReady(false);
    const [x0, y0, x1, y1] = geo.bounds;
    const map = new maplibregl.Map({
      container: el.current,
      style: basemap.style,
      bounds: [
        [x0, y0],
        [x1, y1],
      ],
      fitBoundsOptions: { padding: 28 },
      // Replaced by <Credits>, which collapses smoothly (MapLibre's compact control snaps shut).
      attributionControl: false,
      cooperativeGestures: true,
      canvasContextAttributes: { preserveDrawingBuffer: true },
    });
    mapRef.current = map;
    if (import.meta.env.DEV) window.__map = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    const fit = () => {
      map.resize();
      map.fitBounds([[x0, y0], [x1, y1]], { padding: { top: 28, right: 28, left: 28, bottom: 44 }, duration: 0 });
      if (viz === '3d') map.jumpTo({ pitch: 55, bearing: -14, zoom: map.getZoom() + 0.35 });
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
      if (basemap.hillshade) {
        map.addSource('dem', { type: 'raster-dem', tiles: [TERRAIN], encoding: 'terrarium', tileSize: 256, maxzoom: 12 });
        map.addLayer(
          { id: 'hillshade', type: 'hillshade', source: 'dem', paint: { 'hillshade-exaggeration': 0.45, 'hillshade-shadow-color': C.dark ? '#000000' : '#4a4a42', 'hillshade-highlight-color': C.dark ? '#3a4a44' : '#ffffff' } },
          firstSymbol,
        );
      }

      const holes = outerRings(geo.basin.geometry);
      map.addSource('mask', {
        type: 'geojson',
        data: { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]], ...holes] } },
      });
      // Island hides everything outside the basin, labels included, so the mask goes above the basemap's symbols.
      map.addLayer({ id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': C.paper, 'fill-opacity': VIZ_MASK[viz] } }, viz === 'island' ? undefined : firstSymbol);
      if (viz === '3d') {
        map.addSource('terrain-dem', { type: 'raster-dem', tiles: [TERRAIN], encoding: 'terrarium', tileSize: 256, maxzoom: 12 });
        map.setTerrain({ source: 'terrain-dem', exaggeration: 1.8 });
      }

      map.addSource('wx', { type: 'image', url: blankPng(), coordinates: [[0, 1], [1, 1], [1, 0], [0, 0]] });
      map.addLayer({ id: 'wx', type: 'raster', source: 'wx', paint: { 'raster-opacity': 0.9, 'raster-fade-duration': 0 } }, firstSymbol);
      map.addSource('dots', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      map.addLayer(
        {
          id: 'dots',
          type: 'circle',
          source: 'dots',
          paint: { 'circle-radius': ['get', 'r'], 'circle-color': ['get', 'c'], 'circle-opacity': 0.9, 'circle-stroke-width': 0 },
        },
        firstSymbol,
      );

      map.addSource('basin', { type: 'geojson', data: geo.basin });
      map.addLayer({ id: 'basin-line', type: 'line', source: 'basin', paint: { 'line-color': C.ink, 'line-width': 1.6, 'line-opacity': 0.85 } });

      map.addSource('rivers', { type: 'geojson', data: geo.rivers });
      map.addLayer({
        id: 'rivers',
        type: 'line',
        source: 'rivers',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': viz === 'rivers' ? ['coalesce', ['get', 'c'], C.flow] : C.flow,
          'line-opacity': ['interpolate', ['linear'], ['get', 'order'], 2, viz === 'rivers' ? 0.85 : 0.6, 5, 1],
          'line-width': riverWidth(viz === 'rivers' ? 2.4 : 1),
        },
      });

      const big = { ...geo.dams, features: geo.dams.features.filter((f) => f.properties.storage_af >= 50000) };
      map.addSource('dams', { type: 'geojson', data: big });
      map.addLayer({
        id: 'dams',
        type: 'circle',
        source: 'dams',
        paint: { 'circle-radius': 5, 'circle-color': C.ink, 'circle-stroke-color': C.card, 'circle-stroke-width': 1.5 },
      });
      map.addLayer({
        id: 'dams-label',
        type: 'symbol',
        source: 'dams',
        layout: {
          'text-field': ['get', 'name'],
          'text-font': basemap.fonts.regular,
          'text-size': 11,
          'text-offset': [0, 0.9],
          'text-anchor': 'top',
        },
        paint: { 'text-color': C.muted, 'text-halo-color': C.card, 'text-halo-width': 1.5 },
      });

      map.addSource('gauges', { type: 'geojson', data: geo.gauges });
      map.addLayer({
        id: 'gauges',
        type: 'circle',
        source: 'gauges',
        filter: ['==', ['get', 'active'], true],
        paint: { 'circle-radius': 3.5, 'circle-color': C.card, 'circle-stroke-color': C.flow, 'circle-stroke-width': 1.5 },
      });

      map.addSource('site', { type: 'geojson', data: geo.gauge });
      map.addLayer({ id: 'site-halo', type: 'circle', source: 'site', paint: { 'circle-radius': 13, 'circle-color': C.flow, 'circle-opacity': 0.18 } });
      map.addLayer({ id: 'site', type: 'circle', source: 'site', paint: { 'circle-radius': 6.5, 'circle-color': C.flow, 'circle-stroke-color': C.card, 'circle-stroke-width': 2 } });
      map.addLayer({
        id: 'site-label',
        type: 'symbol',
        source: 'site',
        layout: { 'text-field': meta.short, 'text-font': basemap.fonts.bold, 'text-size': 13, 'text-offset': [0, 1.3], 'text-anchor': 'top' },
        paint: { 'text-color': C.ink, 'text-halo-color': C.card, 'text-halo-width': 2 },
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
  }, [geo, meta, basemap, viz]);

  // Rasters are drawn once per layer and cached, and the other layers are drawn while idle, so switching
  // layers only swaps an image.
  const rasters = useRef(new Map());
  useEffect(() => {
    rasters.current = new Map();
  }, [wx, grid, geo]);
  const paintFor = (l, floor) => fieldPaint(l, grid, wx.fields[l.key], geo.basin.geometry, floor);
  const rasterFor = (l) => {
    let r = rasters.current.get(l.key);
    if (!r) {
      r = renderField(grid, wx.fields[l.key], geo.basin.geometry, paintFor(l));
      rasters.current.set(l.key, r);
    }
    return r;
  };
  const dotPts = useMemo(() => dotGrid(geo), [geo]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    if (!wx) {
      map.getSource('wx').updateImage({ url: blankPng(), coordinates: map.getSource('wx').coordinates });
      return;
    }
    if (viz === 'rivers' || viz === 'dots') {
      map.getSource('wx').updateImage({ url: blankPng(), coordinates: map.getSource('wx').coordinates });
      const values = wx.fields[layer.key];
      // Thin lines need the darker half of the ramp to read.
      const paint = paintFor(layer, viz === 'rivers' ? 0.45 : 0);
      const at = (lon, lat) => sampleGrid(grid, values, lon, lat);
      if (viz === 'rivers') {
        const features = geo.rivers.features.map((f) => {
          const cs = f.geometry.type === 'MultiLineString' ? f.geometry.coordinates.flat() : f.geometry.coordinates;
          const v = at(...cs[Math.floor(cs.length / 2)]);
          return { ...f, properties: { ...f.properties, c: v == null ? null : towardGray(paint(v)) } };
        });
        map.getSource('rivers').setData({ ...geo.rivers, features });
      } else {
        const features = dotPts.map(([lon, lat]) => {
          const v = at(lon, lat);
          const [r, g, b, a] = paint(v ?? 0);
          return { type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: { r: 1 + 4.5 * Math.min(1, a / 0.85), c: `rgb(${r},${g},${b})` } };
        });
        map.getSource('dots').setData({ type: 'FeatureCollection', features });
      }
      return;
    }
    map.getSource('wx').updateImage(rasterFor(layer));
    const idle = window.requestIdleCallback ?? ((cb) => setTimeout(cb, 200));
    const cancel = window.cancelIdleCallback ?? clearTimeout;
    const pending = LAYERS.filter((l) => !rasters.current.has(l.key)).map((l) => idle(() => rasterFor(l)));
    return () => pending.forEach(cancel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, wx, layer, grid, geo, viz, dotPts]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !wx) return;
    const values = wx.fields[layer.key];
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
      <div className="pointer-events-none absolute top-3 left-3 rounded-lg bg-card/90 px-3 py-2 text-xs shadow-sm ring-1 ring-line backdrop-blur">
        <div className="flex items-center gap-2 font-medium">
          <span className="size-2.5 rounded-full" style={{ background: layer.color }} />
          {layer.label}
        </div>
        {range && (
          <div className="mt-1.5 flex items-center gap-2 tabular-nums text-muted">
            <span>{layer.show(range.min)}</span>
            <span className="h-2 w-24 rounded-full" style={{ background: `linear-gradient(90deg, ${layer.ramp[0]}, ${layer.ramp[1]})` }} />
            <span>{layer.show(range.max)}</span>
          </div>
        )}
        <div className="mt-1.5 flex items-center gap-3 text-muted">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 rounded" style={{ background: C.flow }} />
            Rivers
          </span>
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-full border-[1.5px] bg-card" style={{ borderColor: C.flow }} />
            Gauges
          </span>
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-full" style={{ background: C.ink }} />
            Reservoirs
          </span>
        </div>
      </div>
      <Credits key={basemap.label} map={ready ? mapRef.current : null} credits={basemap.credits} hillshade={basemap.hillshade} />
      {hover && wx && (
        <div
          className="pointer-events-none absolute rounded bg-ink px-1.5 py-0.5 text-[11px] font-medium text-card tabular-nums"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
        >
          {layer.show(hover.v)}
        </div>
      )}
    </div>
  );
}

/**
 * Map credits, open at first and collapsing smoothly to an (i) after five seconds or on the first pan, zoom or
 * click, as the OSMF attribution guidelines allow. Clicking (i) opens them again.
 */
function Credits({ map, credits, hillshade }) {
  const ref = useRef(null);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    if (!map) return;
    const close = (e) => e.originalEvent && setOpen(false);
    const events = ['dragstart', 'zoomstart', 'click'];
    events.forEach((ev) => map.on(ev, close));
    // The five seconds count from when the credits are actually on screen, not from page load.
    let timer;
    const io = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      io.disconnect();
      timer = setTimeout(() => setOpen(false), 5000);
    }, { threshold: 1 });
    io.observe(ref.current);
    return () => {
      io.disconnect();
      clearTimeout(timer);
      events.forEach((ev) => map.off(ev, close));
    };
  }, [map]);
  return (
    <div
      ref={ref}
      className="absolute right-2 bottom-2 flex max-w-[calc(100%-1rem)] items-center rounded-full bg-card/90 text-[11px] text-muted shadow-sm ring-1 ring-line backdrop-blur"
    >
      <div
        className="overflow-hidden text-ellipsis whitespace-nowrap transition-[max-width,opacity,translate] duration-200 ease-out"
        style={{ maxWidth: open ? 800 : 0, opacity: open ? 1 : 0, translate: open ? '0' : '8px 0' }}
        aria-hidden={!open}
      >
        <div className="py-1 pr-1 pl-3">
          <MapCredits credits={credits} hillshade={hillshade} />
        </div>
      </div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="grid size-6 shrink-0 place-items-center rounded-full font-serif text-[12px] font-semibold text-muted italic hover:text-ink"
        aria-label={open ? 'Hide map credits' : 'Show map credits'}
        aria-expanded={open}
      >
        i
      </button>
    </div>
  );
}

function MapCredits({ credits, hillshade }) {
  const a = 'underline decoration-dotted underline-offset-2 hover:text-ink';
  return (
    <span>
      Map ©{' '}
      {credits.map(([label, href], i) => (
        <span key={label}>
          {i ? ', ' : ''}
          <a className={a} href={href} target="_blank" rel="noreferrer">
            {label}
          </a>
        </span>
      ))}
      .{hillshade ? ' Terrain: Mapzen / AWS Open Data.' : ''}
    </span>
  );
}

/** How each basin treatment dims what lies outside the basin. */
const VIZ_MASK = { fill: 0.72, rivers: 0.8, dots: 0.8, island: 1, '3d': 0.72 };

export const VIZ = [
  ['fill', 'Fill'],
  ['rivers', 'Rivers'],
  ['dots', 'Dots'],
  ['island', 'Island'],
  ['3d', '3D terrain'],
];

const riverWidth = (k) => [
  'interpolate',
  ['linear'],
  ['zoom'],
  7,
  ['interpolate', ['linear'], ['get', 'order'], 2, 0.7 * k, 4, 1.4 * k, 7, 3.4 * k],
  11,
  ['interpolate', ['linear'], ['get', 'order'], 2, 1.4 * k, 4, 2.6 * k, 7, 6 * k],
];

/** v -> [r, g, b, alpha]: color stretched over the basin's range, opacity from the absolute amount. */
function fieldPaint(l, grid, values, geometry, floor = 0) {
  const { min, max } = fieldRange(grid, values, geometry);
  const span = max - min;
  const colors = C.dark ? [...l.ramp].reverse() : l.ramp;
  return (v) => [...ramp(colors, floor + (1 - floor) * (span > 1e-6 ? (v - min) / span : 0.5)), l.alpha(v)];
}

/** A line color that fades to gray where the amount is negligible, so dry rivers read as plain rivers. */
function towardGray([r, g, b, a]) {
  const k = Math.min(1, a / 0.85);
  const [gr, gg, gb] = hexRgb(C.faint);
  return `rgb(${Math.round(gr + (r - gr) * k)},${Math.round(gg + (g - gg) * k)},${Math.round(gb + (b - gb) * k)})`;
}

/** Evenly spaced points inside the basin for the dot treatment. */
function dotGrid(geo, step = 0.028) {
  const [x0, y0, x1, y1] = geo.bounds;
  const pts = [];
  for (let y = y0 + step / 2; y < y1; y += step) {
    for (let x = x0 + step / 2; x < x1; x += step * 1.3) {
      if (inPolygon(geo.basin.geometry, [x, y])) pts.push([x, y]);
    }
  }
  return pts;
}

/** Range of a field over grid points inside the basin (falling back to all points for tiny basins). */
function fieldRange(grid, values, geometry) {
  const inside = values.filter((v, i) => v != null && inPolygon(geometry, grid.pts[i]));
  const vs = inside.length ? inside : values.filter((v) => v != null);
  return { min: Math.min(...vs), max: Math.max(...vs) };
}

function blankPng() {
  const c = document.createElement('canvas');
  c.width = c.height = 1;
  return c.toDataURL();
}
