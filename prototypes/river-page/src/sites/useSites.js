import { useCallback, useEffect, useState } from 'react';
import { DEFAULT_SITE, forecastStatus, lastSite, loadSites, sitePath, siteFromUrl, wakeForecast } from './sites.js';

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
 * The site in the URL and a way to go to another one. `/` and unknown paths land on the last site viewed, or
 * Callicoon. Query parameters (theme, layout, time travel) carry over between sites.
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
  const go = useCallback((next) => {
    if (next === siteFromUrl()) return;
    history.pushState(null, '', sitePath(next) + location.search);
    window.scrollTo({ top: 0 });
    setId(next);
  }, []);
  return [id, go];
}

function withoutSite(search) {
  const p = new URLSearchParams(search);
  p.delete('site');
  return p.size ? `?${p}` : '';
}

const POLL_MS = 5000;

/**
 * Wakes a site's forecast when the site is visited without one, then polls until it is ready.
 * Returns null for sites that already have a forecast.
 */
export function useWarmup(site) {
  const cold = site && !site.forecast_ready;
  const [state, setState] = useState(null);
  useEffect(() => {
    if (!cold) {
      setState(null);
      return;
    }
    let stop = false;
    let timer = null;
    const apply = (s) => {
      if (stop) return;
      setState((prev) => ({
        ready: !!s.forecast_ready,
        etaS: s.eta_s ?? prev?.etaS ?? null,
        startedAt: s.started_at ? new Date(s.started_at) : (prev?.startedAt ?? new Date()),
        error: null,
      }));
      if (!s.forecast_ready) timer = setTimeout(poll, POLL_MS);
    };
    const fail = (e) => {
      if (stop) return;
      setState((prev) => ({ ...(prev ?? { ready: false, etaS: null, startedAt: new Date() }), error: String(e.message ?? e) }));
      timer = setTimeout(poll, POLL_MS * 2);
    };
    const poll = () => forecastStatus(site.id).then(apply, fail);
    setState({ ready: false, etaS: null, startedAt: new Date(), error: null });
    wakeForecast(site.id).then(apply, fail);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [cold, site?.id]);
  return state;
}
