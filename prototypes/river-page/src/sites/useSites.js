import { useCallback, useEffect, useState } from 'react';
import { canonicalId, lastSite, loadForecast, loadLive, loadSites, needsVisit, sitePath, siteFromUrl, siteStatus, visitSite } from './sites.js';
import { findGauge, knownGauges, loadCatalog, loadGaugesIn, lookupGauge, reasonText } from './gauges.js';

export function useSites() {
  const [state, setState] = useState({ sites: null, defaultId: null, error: null });
  useEffect(() => {
    loadSites().then(
      ({ sites, defaultId }) => setState({ sites, defaultId, error: null }),
      (e) => setState({ sites: null, defaultId: null, error: String(e.message ?? e) }),
    );
  }, []);
  return state;
}

/**
 * The site in the URL and a way to go to another one. `/` lands on the last site viewed; with none, the id is null
 * until the caller sends it to the index's `default`. Query parameters (theme, layout, time travel) carry over.
 */
export function useSiteRoute() {
  const [id, setId] = useState(() => {
    const fromUrl = siteFromUrl();
    if (fromUrl) {
      if (location.pathname !== sitePath(fromUrl)) history.replaceState(null, '', sitePath(fromUrl) + withoutSite(location.search));
      return fromUrl;
    }
    const start = lastSite();
    if (start) history.replaceState(null, '', sitePath(start) + location.search);
    return start;
  });
  useEffect(() => {
    const onPop = () => setId(siteFromUrl());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const go = useCallback((next, { replace = false } = {}) => {
    if (next === siteFromUrl()) return;
    history[replace ? 'replaceState' : 'pushState'](null, '', sitePath(next) + location.search);
    if (!replace) window.scrollTo({ top: 0 });
    setId(next);
  }, []);
  return [id, go];
}

function withoutSite(search) {
  const p = new URLSearchParams(search);
  p.delete('site');
  return p.size ? `?${p}` : '';
}

/**
 * The site object for a route id: an index site (by USGS id or slug), else a catalog gauge (found through ids.json
 * when the map hasn't loaded its tile). `status` is 'loading', 'found', 'unsupported' (a USGS id the catalog doesn't
 * have) or 'missing' (not a USGS id).
 */
export function useSite(id, sites) {
  const indexed = sites?.find((s) => s.id === id || s.slug === id) ?? null;
  const usgs = canonicalId(id);
  const [found, setFound] = useState({ id: null, site: null, done: false });
  useEffect(() => {
    if (!sites || indexed || !usgs || lookupGauge(usgs)) return;
    let stale = false;
    setFound({ id: usgs, site: null, done: false });
    findGauge(usgs).then(
      (site) => !stale && setFound({ id: usgs, site, done: true }),
      () => !stale && setFound({ id: usgs, site: null, done: true }),
    );
    return () => {
      stale = true;
    };
  }, [sites, indexed, usgs]);
  if (!sites || !id) return { site: null, status: 'loading' };
  if (indexed) return { site: indexed, status: 'found' };
  if (!usgs) return { site: null, status: 'missing' };
  const gauge = lookupGauge(usgs);
  if (gauge) return { site: gauge, status: 'found' };
  if (found.id !== usgs || !found.done) return { site: null, status: 'loading' };
  return { site: null, status: 'unsupported' };
}

/** A site's live.json, refetched when `refresh` changes (a new forecast issue). `live` is null before the first forecast. */
export function useLive(site, refresh) {
  const [state, setState] = useState({ id: null, live: null, done: false, error: null });
  useEffect(() => {
    if (!site?.inIndex) return;
    let stale = false;
    loadLive(site.id).then(
      (live) => !stale && setState({ id: site.id, live, done: true, error: null }),
      (e) => !stale && setState({ id: site.id, live: null, done: true, error: String(e.message ?? e) }),
    );
    return () => {
      stale = true;
    };
  }, [site?.id, refresh]);
  return state.id === site?.id ? state : { id: null, live: null, done: false, error: null };
}

export function useForecast(url) {
  const [state, setState] = useState({ url: null, forecast: null, error: null });
  useEffect(() => {
    if (!url) return;
    let stale = false;
    loadForecast(url).then(
      (forecast) => !stale && setState({ url, forecast, error: null }),
      (e) => !stale && setState({ url, forecast: null, error: String(e.message ?? e) }),
    );
    return () => {
      stale = true;
    };
  }, [url]);
  return state.url === url ? state : { url: null, forecast: null, error: null };
}

const POLL_MS = 10_000;
const POLL_LIMIT_MS = 3 * 60_000;

/**
 * The serving README's page flow: POST /api/visit on load (unless the site is always-on or awake for another day),
 * then while it's waking, poll GET /api/status every 10 s for up to 3 minutes. Returns null until the visit answers
 * or when no visit is needed. `paused` is true once polling gave up or the backend reports the site paused.
 */
export function useVisit(site, live, liveDone) {
  const [state, setState] = useState(null);
  const visitable = !!site?.inIndex && site.forecastable && liveDone;
  useEffect(() => {
    setState(null);
    if (!visitable || !needsVisit(site, live)) return;
    let stop = false;
    let timer = null;
    const t0 = Date.now();
    const apply = (r) => {
      if (stop) return;
      const waking = r.status === 'waking';
      const gaveUp = waking && Date.now() - t0 > POLL_LIMIT_MS;
      setState((prev) => ({
        status: r.status,
        forecast: r.forecast ?? null,
        etaS: r.eta_s ?? prev?.etaS ?? null,
        startedAt: r.run?.started ? new Date(r.run.started) : (prev?.startedAt ?? new Date()),
        paused: r.status === 'paused' || gaveUp,
        error: null,
      }));
      if (waking && !gaveUp) timer = setTimeout(poll, POLL_MS);
    };
    const fail = (e) => {
      if (stop) return;
      // The index said forecastable, but the rule excluded the site since: stop and say why.
      if (e.code === 'not_forecastable') {
        const excluded = (rule) => ({ status: 'not_forecastable', reason: reasonText([e.detail ?? 'not_forecastable'], rule), forecast: null, etaS: null, paused: false, error: null });
        loadCatalog().then(
          (index) => !stop && setState(excluded(index.rule)),
          () => !stop && setState(excluded(null)),
        );
        return;
      }
      setState((prev) => ({ ...(prev ?? { status: 'waking', forecast: null, etaS: null, startedAt: new Date(), paused: false }), error: String(e.message ?? e) }));
      if (Date.now() - t0 < POLL_LIMIT_MS) timer = setTimeout(poll, POLL_MS);
    };
    const poll = () => siteStatus(site).then(apply, fail);
    visitSite(site).then(apply, fail);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [site?.id, visitable]);
  return state;
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
