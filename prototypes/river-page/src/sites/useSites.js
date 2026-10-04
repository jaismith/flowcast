import { useCallback, useEffect, useState } from 'react';
import { canonicalId, DEFAULT_SITE, lastSite, loadSites, needsVisit, sitePath, siteFromUrl, siteStatus, visitSite } from './sites.js';
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
 * The site object for a route id: an index site (by USGS id or slug), or any other USGS stream gauge, looked up live.
 * `status` is 'loading', 'found', 'missing' or 'error' (USGS couldn't be asked, so the gauge may well exist).
 */
export function useSite(id, sites) {
  const indexed = sites?.find((s) => s.id === id || s.slug === id) ?? null;
  const usgs = canonicalId(id);
  const [looked, setLooked] = useState({ id: null, site: null, done: false, error: null });
  useEffect(() => {
    if (!sites || indexed || !usgs) return;
    let stale = false;
    setLooked({ id: usgs, site: null, done: false, error: null });
    lookupGauge(usgs).then(
      (site) => !stale && setLooked({ id: usgs, site, done: true, error: null }),
      (e) => !stale && setLooked({ id: usgs, site: null, done: true, error: String(e.message ?? e) }),
    );
    return () => {
      stale = true;
    };
  }, [sites, indexed, usgs]);
  if (!sites) return { site: null, status: 'loading' };
  if (indexed) return { site: indexed, status: 'found' };
  if (!usgs) return { site: null, status: 'missing' };
  if (looked.id !== usgs || !looked.done) return { site: null, status: 'loading' };
  if (looked.error) return { site: null, status: 'error', error: looked.error };
  return looked.site ? { site: looked.site, status: 'found' } : { site: null, status: 'missing' };
}

const POLL_MS = 10_000;
const POLL_LIMIT_MS = 3 * 60_000;

/**
 * The serving README's page flow: POST /api/visit on load (unless the site is always-on), then while the site is
 * waking, poll GET /api/status every 10 s for up to 3 minutes. Returns null until the visit answers.
 * `paused` is true once polling gave up or the backend reports the site paused.
 */
export function useVisit(site) {
  const [state, setState] = useState(null);
  const eligible = !!site?.forecastable;
  useEffect(() => {
    setState(null);
    if (!eligible || !needsVisit(site)) return;
    let stop = false;
    let timer = null;
    const t0 = Date.now();
    const apply = (r) => {
      if (stop) return;
      const waking = r.state === 'waking';
      const gaveUp = waking && Date.now() - t0 > POLL_LIMIT_MS;
      setState((prev) => ({
        state: r.state,
        forecast: r.forecast ?? null,
        etaS: r.eta_s ?? prev?.etaS ?? null,
        startedAt: r.run?.started ? new Date(r.run.started) : (prev?.startedAt ?? new Date()),
        paused: r.state === 'paused' || gaveUp,
        error: null,
      }));
      if (waking && !gaveUp) timer = setTimeout(poll, POLL_MS);
    };
    const fail = (e) => {
      if (stop) return;
      setState((prev) => ({ ...(prev ?? { state: 'waking', forecast: null, etaS: null, startedAt: new Date(), paused: false }), error: String(e.message ?? e) }));
      if (Date.now() - t0 < POLL_LIMIT_MS) timer = setTimeout(poll, POLL_MS);
    };
    const poll = () => siteStatus(site).then(apply, fail);
    visitSite(site).then(apply, fail);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [site?.id, eligible]);
  return state;
}

/** Below this zoom the map shows only index sites; above it, every active USGS stream gauge in view. */
export const GAUGE_MIN_ZOOM = 7.5;

/** Every USGS gauge fetched so far, refreshed as the map view moves. */
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
