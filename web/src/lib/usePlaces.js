import { useEffect, useState } from 'react';
import { findGauge, knownGauges, loadGaugesIn, loadPlaces, lookupGauge } from './places.ts';

/** Every sites.json site as a Place, and the index's default site. */
export function usePlaces() {
  const [state, setState] = useState({ places: null, defaultId: null, error: null });
  useEffect(() => {
    loadPlaces().then(
      ({ places, defaultId }) => setState({ places, defaultId, error: null }),
      (e) => setState({ places: null, defaultId: null, error: String(e.message ?? e) }),
    );
  }, []);
  return state;
}

/**
 * The Place for a route: an index site (by USGS id or slug), else a catalog gauge (found through ids.json when the
 * map hasn't loaded its tile). `status` is 'loading', 'found', 'unsupported' (a USGS id the catalog doesn't have)
 * or 'missing' (not a USGS id or a slug).
 */
export function usePlace(route, places) {
  const id = route.site ?? null;
  const indexed = places?.find((s) => (id ? s.id === id : s.slug === route.slug)) ?? null;
  const [found, setFound] = useState({ id: null, place: null, done: false });
  useEffect(() => {
    if (!places || indexed || !id || lookupGauge(id)) return;
    let stale = false;
    setFound({ id, place: null, done: false });
    findGauge(id).then(
      (place) => !stale && setFound({ id, place, done: true }),
      () => !stale && setFound({ id, place: null, done: true }),
    );
    return () => {
      stale = true;
    };
  }, [places, indexed, id]);
  if (!places || route.site === null) return { place: null, status: 'loading' };
  if (indexed) return { place: indexed, status: 'found' };
  if (!id) return { place: null, status: 'missing' };
  const gauge = lookupGauge(id);
  if (gauge) return { place: gauge, status: 'found' };
  if (found.id !== id || !found.done) return { place: null, status: 'loading' };
  return { place: null, status: 'unsupported' };
}

/** Below this zoom the map shows only the flowcast sites; above it, every catalog gauge in view. */
export const GAUGE_MIN_ZOOM = 6;

/** Every catalog gauge loaded so far, refreshed as the map view moves into new tiles. */
export function useGaugesInView(view) {
  const [state, setState] = useState({ gauges: [...knownGauges().values()], loading: false, error: null });
  const bboxKey = view && view.zoom >= GAUGE_MIN_ZOOM ? view.bounds.map((v) => v.toFixed(2)).join(',') : null;
  useEffect(() => {
    if (!bboxKey) {
      setState((s) => ({ ...s, loading: false, error: null }));
      return;
    }
    let stale = false;
    const t = setTimeout(() => {
      setState((s) => ({ ...s, loading: true, error: null }));
      loadGaugesIn(bboxKey.split(',').map(Number)).then(
        () => !stale && setState({ gauges: [...knownGauges().values()], loading: false, error: null }),
        (e) => !stale && setState((s) => ({ ...s, loading: false, error: String(e.message ?? e) })),
      );
    }, 300);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [bboxKey]);
  return state;
}
