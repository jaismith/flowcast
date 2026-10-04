import { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import { C } from '../lib/palette.js';

const FONT = ['Noto Sans Regular'];
const HOVER = ['boolean', ['feature-state', 'hover'], false];

/**
 * Every gauge flowcast covers. Solid dots have a forecast ready; hollow ones start one when opened. Sites that
 * don't match the search fade back, and the view follows the matches.
 */
export default function SiteMap({ sites, matches, current, hover, onHover, onSelect, open }) {
  const el = useRef(null);
  const mapRef = useRef(null);
  const [loaded, setLoaded] = useState(false);
  const handlers = useRef({ onHover, onSelect });
  handlers.current = { onHover, onSelect };

  useEffect(() => {
    const map = new maplibregl.Map({
      container: el.current,
      style: `https://tiles.openfreemap.org/styles/${C.basemap}`,
      bounds: bounds(sites),
      fitBoundsOptions: { padding: 40, maxZoom: 8 },
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    mapRef.current = map;
    if (import.meta.env.DEV) window.__siteMap = map;

    map.on('load', () => {
      map.addSource('sites', { type: 'geojson', data: features(sites, new Set(sites.map((s) => s.id)), current), promoteId: 'id' });
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
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, ['case', HOVER, 7, 4.5], 9, ['case', HOVER, 9.5, 7]],
          'circle-color': ['case', ['get', 'ready'], C.flow, C.card],
          'circle-stroke-color': ['case', ['get', 'ready'], C.card, C.flow],
          'circle-stroke-width': ['case', ['get', 'ready'], 1.5, 2],
          'circle-opacity': ['case', ['get', 'match'], 1, 0.25],
          'circle-stroke-opacity': ['case', ['get', 'match'], 1, 0.25],
        },
      });
      map.addLayer({
        id: 'sites-label',
        type: 'symbol',
        source: 'sites',
        filter: ['get', 'match'],
        layout: {
          'text-field': ['get', 'town'],
          'text-font': FONT,
          'text-size': 11.5,
          'text-anchor': 'left',
          'text-offset': [0.9, 0],
          'text-optional': true,
        },
        paint: { 'text-color': C.ink, 'text-halo-color': C.paper, 'text-halo-width': 1.6 },
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
      setLoaded(true);
    });
    return () => map.remove();
    // The map is built once; sites, matches and hover are pushed into it below.
  }, []);

  const matchKey = matches.map((s) => s.id).join(',');
  useEffect(() => {
    const map = mapRef.current;
    if (!loaded) return;
    map.getSource('sites').setData(features(sites, new Set(matches.map((s) => s.id)), current));
    if (matches.length) map.fitBounds(bounds(matches), { padding: 48, maxZoom: matches.length === 1 ? 9 : 8, duration: 450 });
  }, [loaded, matchKey, current]);

  const prevHover = useRef(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!loaded) return;
    if (prevHover.current) map.setFeatureState({ source: 'sites', id: prevHover.current }, { hover: false });
    if (hover) map.setFeatureState({ source: 'sites', id: hover }, { hover: true });
    prevHover.current = hover;
  }, [loaded, hover]);

  useEffect(() => {
    if (open) mapRef.current?.resize();
  }, [open]);

  return <div ref={el} className="size-full" />;
}

function features(sites, match, current) {
  return {
    type: 'FeatureCollection',
    features: sites.map((s) => ({
      type: 'Feature',
      properties: { id: s.id, town: s.town, ready: !!s.forecast_ready, match: match.has(s.id), current: s.id === current },
      geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
    })),
  };
}

function bounds(sites) {
  const lons = sites.map((s) => s.lon);
  const lats = sites.map((s) => s.lat);
  const pad = sites.length === 1 ? 0.25 : 0;
  return [
    [Math.min(...lons) - pad, Math.min(...lats) - pad],
    [Math.max(...lons) + pad, Math.max(...lats) + pad],
  ];
}
