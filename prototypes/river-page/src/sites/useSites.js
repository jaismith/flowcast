import { useCallback, useEffect, useState } from 'react';
import { canonicalId, DEFAULT_SITE, lastSite, loadForecast, loadLive, loadSites, needsVisit, sitePath, siteFromUrl, siteStatus, visitSite } from './sites.js';
import { knownGauges, loadGaugesIn, lookupGauge } from './gauges.js';

export function useSites() {
  const [state, setState] = useState({ sites: null, error: null });
  useEffect(() => {
    loadSites().then(
      (sites) => setState({ sites, error: null }),
      (e) => setState({ sites: null, error: String(e.message ?? e) }),
    );
  }, []);
  return state;
}

/**
 * The site in the URL and a way to go to another one. `/` lands on the last site viewed, or Callicoon. Query
 * parameters (theme, layout, time travel) carry over between sites.
 */
export function useSiteRoute() {
  const [id, setId] = useState(() => {
    const fromUrl = siteFromUrl();
    if (fromUrl) {
      if (location.pathname !== sitePath(fromUrl)) history.replaceState(null, '', sitePath(fromUrl) + withoutSite(location.search));
      return fromUrl;
    }
    const start = lastSite() ?? DEFAULT_SITE;
    history.replaceState(null, '', sitePath(start) + location.search);
    return start;
  });
  useEffect(() => {
    const onPop = () => setId(siteFromUrl() ?? DEFAULT_SITE);
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
 * The site object for a route id: an index site (by USGS id or slug), or a catalog gauge already loaded by the map.
 * `status` is 'loading', 'found', 'unsupported' (a USGS id flowcast doesn't forecast) or 'missing' (not a USGS id).
 */
export function useSite(id, sites) {
  if (!sites) return { site: null, status: 'loading' };
  const indexed = sites.find((s) => s.id === id || s.slug === id);
  if (indexed) return { site: indexed, status: 'found' };
  const usgs = canonicalId(id);
  if (!usgs) return { site: null, status: 'missing' };
  const gauge = lookupGauge(usgs);
  return gauge ? { site: gauge, status: 'found' } : { site: null, status: 'unsupported' };
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
  const visitable = !!site?.inIndex && liveDone;
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
