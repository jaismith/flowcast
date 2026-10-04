// Writes a trimmed copy of the national gauge catalog (serving/schema/gauges.schema.json) into the dev fixtures:
// the index, and the catalog gauges within RADIUS_DEG of each fixture site, so the site map in `npm run dev` has
// gauges around Callicoon, Allagash and Accotink. Gauges the backend can forecast that aren't fixture sites are
// rewritten as `eligible` (not forecast yet), since the mock only serves the fixture sites.
//
//   node scripts/make-gauge-fixtures.mjs [--from https://d2plrkhnzsjv1y.cloudfront.net]
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => (v.startsWith('--') ? [...a, [v.slice(2), all[i + 1]]] : a), []));
const ORIGIN = args.from ?? 'https://d2plrkhnzsjv1y.cloudfront.net';
const OUT = path.join(path.dirname(new URL(import.meta.url).pathname), '../dev/fixtures/data/v1');
const RADIUS_DEG = 1.5;

const get = async (p) => {
  const r = await fetch(ORIGIN + p);
  if (!r.ok) throw new Error(`${p}: HTTP ${r.status}`);
  return r.json();
};
const write = (p, obj) => {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj));
  console.log(`${path.relative(process.cwd(), p)}: ${(fs.statSync(p).size / 1e3).toFixed(0)} kB`);
};

const sites = JSON.parse(fs.readFileSync(path.join(OUT, 'sites.json'), 'utf8')).sites;
const served = new Set(sites.filter((s) => s.forecastable).map((s) => s.id));
const near = (g) => sites.some((s) => Math.abs(s.lat - g.lat) <= RADIUS_DEG && Math.abs(s.lon - g.lon) <= RADIUS_DEG);
const index = await get('/data/v1/gauges/index.json');
const step = index.tile_deg;
const keys = [...new Set(sites.map((s) => `${Math.floor(s.lon / step) * step}_${Math.floor(s.lat / step) * step}`))].filter((k) => k in index.tiles);

fs.rmSync(path.join(OUT, 'gauges'), { recursive: true, force: true });
const ids = {};
const tiles = {};
for (const key of keys) {
  const tile = await get(index.tile_url.replace('{key}', key));
  const gauges = tile.gauges.filter(near).map((g) =>
    g.eligibility.forecast_now && !served.has(g.id) ? { ...g, model_basin: false, eligibility: { status: 'eligible', forecast_now: false, reasons: [] } } : g,
  );
  for (const g of gauges) ids[g.id] = key;
  tiles[key] = gauges.length;
  write(path.join(OUT, 'gauges', 'tiles', `${key}.json`), { ...tile, gauges });
}
write(path.join(OUT, 'gauges', 'index.json'), { ...index, tiles });
write(path.join(OUT, 'gauges', 'ids.json'), { schema: 'flowcast.gauges.ids/v1', generated: index.generated, tiles: ids });
