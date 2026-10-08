import { useEffect, useState } from 'react';

// '/' in production, '/preview/<name>/' for a preview deploy (vite --base).
const BASE = import.meta.env.BASE_URL;
const NUM = '[0-9]{8,15}';
const LAST_SITE_KEY = 'flowcast:last-site';

/** The canonical page of a site; ids carry their agency prefix, as in sites.json (USGS-01427510). */
export const sitePath = (id) => `${BASE}site/${id}`;

/** "01427510", "usgs-01427510" or "USGS-01427510" → "USGS-01427510"; anything else → null. */
export function canonicalId(raw) {
  const m = new RegExp(`^(?:usgs-?)?(${NUM})$`, 'i').exec(String(raw ?? '').trim());
  return m ? `USGS-${m[1]}` : null;
}

/**
 * Route for a URL: `{ site: null }` for `/`, `{ site }` for `/site/USGS-<number>`, `{ slug }` for anything else
 * under `/site/` (a friendly alias such as `/site/callicoon`, resolved against sites.json, or a typo), `{ redirect }`
 * for older links (`?site=<id>`, `/site/<number>`, a bare `/<number>` or `/USGS-<number>`, any case), or
 * `{ invalid }` with the path.
 */
function parse({ pathname, search }) {
  const path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname.replace(/^\//, '');
  const params = new URLSearchParams(search);
  const legacy = params.get('site');
  params.delete('site');
  const keep = params.size ? `?${params}` : '';
  const canonical = (id) => sitePath(id) + keep;
  if (path === '' || path === 'index.html') {
    const id = canonicalId(legacy);
    return id ? { redirect: canonical(id) } : { site: null };
  }
  let m = path.match(new RegExp(`^site/(USGS-${NUM})/?$`));
  if (m) return { site: m[1] };
  m = path.match(new RegExp(`^(?:site/)?((?:USGS-?)?${NUM})/?$`, 'i'));
  if (m) return { redirect: canonical(canonicalId(m[1])) };
  m = path.match(/^site\/([^/]+)\/?$/);
  if (m) return { slug: decodeURIComponent(m[1]) };
  return { invalid: decodeURIComponent(path) };
}

/** Moves to a path within the app without a reload; query parameters carry over. A new page starts at the top. */
export function navigate(path, { replace = false } = {}) {
  const next = path + location.search;
  if (next === location.pathname + location.search) return;
  history[replace ? 'replaceState' : 'pushState'](null, '', next);
  if (!replace) window.scrollTo({ top: 0 });
  dispatchEvent(new PopStateEvent('popstate'));
}

export function useRoute() {
  const resolve = () => {
    const r = parse(location);
    if (!r.redirect) return r;
    history.replaceState(null, '', r.redirect);
    return parse(location);
  };
  const [route, setRoute] = useState(resolve);
  useEffect(() => {
    const on = () => setRoute(resolve());
    addEventListener('popstate', on);
    return () => removeEventListener('popstate', on);
  }, []);
  return route;
}

/** The last forecastable site opened here, which `/` lands on before falling back to sites.json's `default`. */
export function lastSite() {
  try {
    return canonicalId(localStorage.getItem(LAST_SITE_KEY));
  } catch {
    return null;
  }
}

export function rememberSite(id) {
  try {
    localStorage.setItem(LAST_SITE_KEY, id);
  } catch {
    // private mode; the default site is fine
  }
}
