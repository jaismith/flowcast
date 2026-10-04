import { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import { C } from '../lib/palette.js';

const FONT = ['Noto Sans Regular'];
const HOVER = ['boolean', ['feature-state', 'hover'], false];
const MATCH = ['get', 'match'];
const KIND = ['get', 'kind'];

/** How a site is drawn: solid once a forecast exists, a ring while it would start on opening, a speck when it can't. */
export const kindOf = (s) => (!s.forecastable ? 'none' : s.hasForecast ? 'ready' : s.temperature ? 'start' : 'flow');

/**
 * Index sites and every USGS gauge fetched for the view. Reports the view through `onView` so the caller can fetch
 * gauges for it; `fit` (a key and a list of sites) frames search matches, and `flyTo` pans to one site.
 */
export default function SiteMap({ sites, matchIds, current, start, hover, onHover, onSelect, onView, open, fit, flyTo }) {
  const el = useRef(null);
  const mapRef = useRef(null);
  const [loaded, setLoaded] = useState(false);
  // Each keystroke yields a new fit, but restarting an animation toward the same place makes it stutter.
  const lastFit = useRef(null);
  const handlers = useRef({ onHover, onSelect, onView });
  handlers.current = { onHover, onSelect, onView };

  useEffect(() => {
    const map = new maplibregl.Map({
      container: el.current,
      style: `https://tiles.openfreemap.org/styles/${C.basemap}`,
      ...(start ? { center: [start.lon, start.lat], zoom: 8 } : { bounds: bounds(sites), fitBoundsOptions: { padding: 40, maxZoom: 8 } }),
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    mapRef.current = map;
    if (import.meta.env.DEV) window.__siteMap = map;
    const report = () => {
      const b = map.getBounds();
      handlers.current.onView({ bounds: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: map.getZoom(), center: map.getCenter() });
    };

    map.on('load', () => {
      map.addSource('sites', { type: 'geojson', data: features(sites, matchIds, current), promoteId: 'id' });
      map.addLayer({
        id: 'sites-current',
        type: 'circle',
        source: 'sites',
        filter: ['get', 'current'],
        paint: { 'circle-radius': 14, 'circle-color': C.flow, 'circle-opacity': 0.14, 'circle-stroke-color': C.flow, 'circle-stroke-width': 1, 'circle-stroke-opacity': 0.5 },
      });
      map.addLayer({
        id: 'sites',
        type: 'circle',
        source: 'sites',
        layout: { 'circle-sort-key': ['match', KIND, 'ready', 3, 'start', 2, 'flow', 1, 0] },
        paint: {
          'circle-radius': [
            'interpolate',
            ['linear'],
            ['zoom'],
            4,
            ['case', ['==', KIND, 'none'], 2, HOVER, 7, 4.5],
            10,
            ['case', ['==', KIND, 'none'], ['case', HOVER, 5, 3.5], HOVER, 9.5, 7],
          ],
          'circle-color': ['match', KIND, 'ready', C.flow, 'none', C.faint, C.card],
          'circle-stroke-color': ['match', KIND, 'ready', C.card, C.flow],
          'circle-stroke-width': ['match', KIND, 'ready', 1.5, 'start', 2, 'flow', 1.6, 0],
          'circle-opacity': ['case', MATCH, ['match', KIND, 'none', 0.75, 1], 0.2],
          'circle-stroke-opacity': ['case', MATCH, ['match', KIND, 'flow', 0.5, 1], 0.2],
        },
      });
      const label = { 'text-font': FONT, 'text-size': 11.5, 'text-anchor': 'left', 'text-offset': [0.9, 0], 'text-optional': true };
      const ink = { 'text-color': C.ink, 'text-halo-color': C.paper, 'text-halo-width': 1.6 };
      map.addLayer({ id: 'sites-label', type: 'symbol', source: 'sites', filter: ['all', MATCH, ['get', 'indexed']], layout: { ...label, 'text-field': ['get', 'town'] }, paint: ink });
      map.addLayer({
        id: 'gauges-label',
        type: 'symbol',
        source: 'sites',
        minzoom: 9.5,
        filter: ['all', MATCH, ['!', ['get', 'indexed']], ['!=', KIND, 'none']],
        layout: { ...label, 'text-field': ['get', 'town'], 'text-size': 11 },
        paint: { ...ink, 'text-color': C.muted },
      });
      map.on('mousemove', 'sites', (e) => {
        map.getCanvas().style.cursor = 'pointer';
        handlers.current.onHover(e.features[0].properties.id);
      });
      map.on('mouseleave', 'sites', () => {
        map.getCanvas().style.cursor = '';
        handlers.current.onHover(null);
      });
      map.on('click', 'sites', (e) => handlers.current.onSelect(e.features[0].properties.id));
      map.on('moveend', report);
      map.on('movestart', (e) => {
        if (e.originalEvent) lastFit.current = null;
      });
      report();
      setLoaded(true);
    });
    return () => map.remove();
    // The map is built once; sites, matches and hover are pushed into it below.
  }, []);

  useEffect(() => {
    if (loaded) mapRef.current.getSource('sites').setData(features(sites, matchIds, current));
  }, [loaded, sites, matchIds, current]);

  useEffect(() => {
    if (!loaded) return;
    if (!fit?.sites.length) {
      lastFit.current = null;
      return;
    }
    const b = bounds(fit.sites);
    const maxZoom = fit.sites.length === 1 ? 10 : 9;
    const target = `${b.flat().map((v) => v.toFixed(4))},${maxZoom}`;
    if (target === lastFit.current) return;
    lastFit.current = target;
    mapRef.current.fitBounds(b, { padding: 48, maxZoom, duration: 450 });
  }, [loaded, fit?.key]);

  useEffect(() => {
    if (loaded && flyTo) mapRef.current.flyTo({ center: [flyTo.lon, flyTo.lat], zoom: Math.max(mapRef.current.getZoom(), 9), duration: 700 });
  }, [loaded, flyTo]);

  const prevHover = useRef(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!loaded) return;
    if (prevHover.current && map.getSource('sites')) map.setFeatureState({ source: 'sites', id: prevHover.current }, { hover: false });
    if (hover) map.setFeatureState({ source: 'sites', id: hover }, { hover: true });
    prevHover.current = hover;
  }, [loaded, hover]);

  useEffect(() => {
    if (open) mapRef.current?.resize();
  }, [open]);

  return <div ref={el} className="size-full" />;
}

function features(sites, matchIds, current) {
  return {
    type: 'FeatureCollection',
    features: sites.map((s) => ({
      type: 'Feature',
      properties: { id: s.id, town: s.town ?? s.river, kind: kindOf(s), indexed: s.inIndex, match: !matchIds || matchIds.has(s.id), current: s.id === current },
      geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
    })),
  };
}

function bounds(sites) {
  const lons = sites.map((s) => s.lon);
  const lats = sites.map((s) => s.lat);
  const pad = sites.length === 1 ? 0.1 : 0;
  return [
    [Math.min(...lons) - pad, Math.min(...lats) - pad],
    [Math.max(...lons) + pad, Math.max(...lats) + pad],
  ];
}
