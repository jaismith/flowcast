// Dev-only stand-in for the forecast backend: serves dev/fixtures as /data/ and fakes /api/visit and /api/status.
// Never part of a build. FLOWCAST_MOCK picks how sites behave until visited:
//   ready (default)  every site has a current forecast
//   snoozed          sites are snoozed; a visit warms them for WARM_MS, then they are ready
//   new              like snoozed, but the bundle 404s until the first forecast is ready
import fs from 'node:fs';
import path from 'node:path';

const FIXTURES = path.join(path.dirname(new URL(import.meta.url).pathname), 'fixtures');
const WARM_MS = 20000;

export default function mockApi() {
  const mode = process.env.FLOWCAST_MOCK ?? 'ready';
  const visits = new Map();
  const state = (id) => {
    if (mode === 'ready') return 'ready';
    const t = visits.get(id);
    if (t == null) return 'snoozed';
    return Date.now() - t < WARM_MS ? 'warming' : 'ready';
  };
  const send = (res, code, body) => {
    res.statusCode = code;
    res.setHeader('Content-Type', 'application/json');
    res.end(body == null ? '' : typeof body === 'string' ? body : JSON.stringify(body));
  };
  return {
    name: 'flowcast-mock-api',
    apply: 'serve',
    configureServer(server) {
      server.config.logger.info(`  flowcast mock backend: FLOWCAST_MOCK=${mode}`);
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url, 'http://localhost');
        if (url.pathname === '/api/visit' && req.method === 'POST') {
          let body = '';
          req.on('data', (c) => (body += c));
          req.on('end', () => {
            const id = JSON.parse(body || '{}').site;
            if (id && !visits.has(id)) visits.set(id, Date.now());
            send(res, 202, { ok: true });
          });
          return;
        }
        if (url.pathname === '/api/status') {
          const id = url.searchParams.get('site');
          const s = state(id);
          const file = path.join(FIXTURES, 'sites', `${id}.json`);
          if (!fs.existsSync(file)) return send(res, 404, { error: 'unknown site' });
          const issued = mode === 'new' && s !== 'ready' ? null : JSON.parse(fs.readFileSync(file, 'utf8')).flow_forecast?.issued_at ?? null;
          const eta = s === 'warming' ? Math.ceil((WARM_MS - (Date.now() - visits.get(id))) / 1000) : null;
          return send(res, 200, { state: s, issued_at: issued, eta_s: eta });
        }
        const m = url.pathname.match(/^\/data\/(sites\.json|sites\/([0-9]+)\.json)$/);
        if (m) {
          const file = path.join(FIXTURES, m[1]);
          if (!fs.existsSync(file) || (mode === 'new' && m[2] && state(m[2]) !== 'ready')) return send(res, 404, { error: 'not found' });
          return send(res, 200, fs.readFileSync(file, 'utf8'));
        }
        next();
      });
    },
  };
}
