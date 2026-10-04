import { useEffect, useState } from 'react';

// '/' in production, '/preview/<name>/' for a preview deploy (vite --base).
const BASE = import.meta.env.BASE_URL;
const ID = '[0-9]{8,15}';

/** The canonical page of a site (ids are bare USGS site numbers, as in sites.json). */
export const sitePath = (id) => `${BASE}site/USGS-${id}`;

/**
 * Route for a URL: `{ site: null }` for `/`, `{ site }` for `/site/USGS-<id>`, `{ redirect }` for older links
 * (`?site=<id>`, `/site/<id>`, a bare `/<id>` or `/USGS-<id>`), or `{ invalid: true }`.
 */
function parse({ pathname, search }) {
  const path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname.replace(/^\//, '');
  const legacy = new URLSearchParams(search).get('site');
  const keep = (() => {
    const p = new URLSearchParams(search);
    p.delete('site');
    return p.size ? `?${p}` : '';
  })();
  if (path === '' || path === 'index.html') {
    if (legacy && new RegExp(`^(USGS-)?${ID}$`).test(legacy)) return { redirect: sitePath(legacy.replace(/^USGS-/, '')) + keep };
    return { site: null };
  }
  const canonical = path.match(new RegExp(`^site/USGS-(${ID})/?$`));
  if (canonical) return { site: canonical[1] };
  const old = path.match(new RegExp(`^(?:site/)?(?:USGS-)?(${ID})/?$`));
  if (old) return { redirect: sitePath(old[1]) + keep };
  return { invalid: true };
}

/** Moves to a path within the app without a reload. */
export function navigate(path) {
  history.pushState(null, '', path);
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
