import { useEffect, useState } from 'react';

// '/' in production, '/preview/<name>/' for a preview deploy (vite --base).
const BASE = import.meta.env.BASE_URL;

/** The site id in `/site/<USGS-id>`, or null for `/`; `invalid` for any other path. */
function parse(pathname) {
  const path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname.replace(/^\//, '');
  if (path === '' || path === 'index.html') return { site: null };
  const m = path.match(/^site\/([0-9]{8,15})\/?$/);
  return m ? { site: m[1] } : { invalid: true };
}

export const sitePath = (id) => `${BASE}site/${id}`;

/** Moves to a path within the app without a reload. */
export function navigate(path) {
  history.pushState(null, '', path);
  dispatchEvent(new PopStateEvent('popstate'));
}

export function useRoute() {
  const [route, setRoute] = useState(() => parse(location.pathname));
  useEffect(() => {
    const on = () => setRoute(parse(location.pathname));
    addEventListener('popstate', on);
    return () => removeEventListener('popstate', on);
  }, []);
  return route;
}
