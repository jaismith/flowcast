import { useEffect, useState } from 'react';

// '/' in production, '/preview/<name>/' for a preview deploy (vite --base).
const BASE = import.meta.env.BASE_URL;
const NUM = '[0-9]{8,15}';

/** The canonical page of a site; ids carry their agency prefix, as in sites.json (USGS-01427510). */
export const sitePath = (id) => `${BASE}site/${id}`;

/**
 * Route for a URL: `{ site: null }` for `/`, `{ site }` for `/site/USGS-<number>`, `{ slug }` for a friendly
 * alias (`/site/callicoon`, resolved against sites.json), `{ redirect }` for older links (`?site=<id>`,
 * `/site/<number>`, a bare `/<number>` or `/USGS-<number>`), or `{ invalid: true }`.
 */
function parse({ pathname, search }) {
  const path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname.replace(/^\//, '');
  const params = new URLSearchParams(search);
  const legacy = params.get('site');
  params.delete('site');
  const keep = params.size ? `?${params}` : '';
  const canonical = (n) => sitePath(`USGS-${n}`) + keep;
  if (path === '' || path === 'index.html') {
    const m = legacy?.match(new RegExp(`^(?:USGS-)?(${NUM})$`));
    return m ? { redirect: canonical(m[1]) } : { site: null };
  }
  let m = path.match(new RegExp(`^site/(USGS-${NUM})/?$`));
  if (m) return { site: m[1] };
  m = path.match(new RegExp(`^(?:site/)?(?:USGS-)?(${NUM})/?$`));
  if (m) return { redirect: canonical(m[1]) };
  m = path.match(/^site\/([a-z0-9-]+)\/?$/);
  if (m) return { slug: m[1] };
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
