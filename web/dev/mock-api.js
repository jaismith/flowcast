// Dev-only stand-in for the serving backend (contract v1, PR #64): serves dev/fixtures/data/v1 as /data/v1/ and
// fakes POST /api/visit and GET /api/status. Never part of a build. FLOWCAST_MOCK picks how sites behave:
//   ready (default)  always-on and active: no visit needed
//   snoozed          a visit wakes the site: waking for WAKE_MS, then active with the same forecast
//   new              like snoozed, but the site has never been forecast until the wake finishes
//   paused           the visit cap is reached: the visit answers paused
//   delayed          active, but the newest forecast is late
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.join(path.dirname(new URL(import.meta.url).pathname), 'fixtures/data/v1');
const WAKE_MS = 20000;

export default function mockApi() {
  const mode = process.env.FLOWCAST_MOCK ?? 'ready';
  const woke = new Map();
  const readLive = (id) => {
    const file = path.join(ROOT, 'sites', id, 'live.json');
    return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
  };
  const status = (id) => {
    if (mode === 'ready' || mode === 'delayed' || mode === 'paused') return mode === 'ready' ? 'active' : mode;
    const t = woke.get(id);
    if (t == null) return 'snoozed';
    return Date.now() - t < WAKE_MS ? 'waking' : 'active';
  };
  /** live.json as the backend would write it in this mode. */
  const live = (id) => {
    const l = readLive(id);
    if (!l || mode === 'ready') return l;
    const s = status(id);
    const awake = s === 'active' && woke.has(id) ? new Date(woke.get(id) + 7 * 86400e3).toISOString() : null;
    // The fixture forecasts are from 2021; a snoozed site's last run is reported as 6 h old so the page keeps
    // drawing it under the "updating" banner, as it would for a recent run.
    const recent = mode === 'snoozed' && l.forecast ? { ...l.forecast, issue_time: new Date(Date.now() - 6 * 3600e3).toISOString().replace(/\.\d+Z$/, 'Z') } : l.forecast;
    const forecast = mode === 'new' && s !== 'active' ? null : recent;
    return { ...l, status: s, always_on: false, awake_until: awake, forecast, recent_forecasts: forecast ? l.recent_forecasts : [] };
  };
  const answer = (id) => {
    const l = live(id);
    const s = l.status;
    return { id, status: s, awake_until: l.awake_until, forecast: l.forecast, eta_s: s === 'waking' ? Math.ceil((WAKE_MS - (Date.now() - woke.get(id))) / 1000) : null };
  };
  const send = (res, code, body, cache = 'no-store') => {
    res.statusCode = code;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', cache);
    res.end(typeof body === 'string' ? body : JSON.stringify(body));
  };
  return {
    name: 'flowcast-mock-api',
    apply: 'serve',
    configureServer(server) {
      server.config.logger.info(`  flowcast mock backend (contract v1): FLOWCAST_MOCK=${mode}`);
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url, 'http://localhost');
        const id = url.searchParams.get('site');
        if (url.pathname === '/api/visit' || url.pathname === '/api/status') {
          if (!id || !readLive(id)) return send(res, 404, { error: 'unknown site' });
          if (url.pathname === '/api/visit') {
            if (req.method !== 'POST') return send(res, 405, { error: 'POST only' });
            if ((mode === 'snoozed' || mode === 'new') && !woke.has(id)) woke.set(id, Date.now());
          }
          return send(res, 200, answer(id));
        }
        const m = url.pathname.match(/^\/data\/v1\/(.+\.json)$/);
        if (!m) return next();
        const live_ = m[1].match(/^sites\/(USGS-[0-9]+)\/live\.json$/);
        if (live_) {
          const l = live(live_[1]);
          return l ? send(res, 200, l, 'max-age=60') : send(res, 404, { error: 'not found' });
        }
        const file = path.join(ROOT, m[1]);
        if (!file.startsWith(ROOT) || !fs.existsSync(file)) return send(res, 404, { error: 'not found' });
        send(res, 200, fs.readFileSync(file, 'utf8'), m[1].includes('/forecasts/') ? 'max-age=31536000, immutable' : 'max-age=60');
      });
    },
  };
}
