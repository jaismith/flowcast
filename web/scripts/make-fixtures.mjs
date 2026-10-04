// Writes dev fixtures in the production contract (src/lib/contract.ts) from the river-page prototype's data, as if
// each site's forecast had just been issued at `--at`. Dev only: `npm run dev` serves them in place of the backend.
//
//   node scripts/make-fixtures.mjs --from <flowcast checkout on cursor/simple-site-page-4752> [--at 2021-06-29T12:00:00Z]
//
// Observations stop at the issue time, as they would live. The prototype has no forecast snowmelt, so melt_mm is
// taken from the observed snowpack's decline over the week (what happened), standing in for the backend's forecast.
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => (v.startsWith('--') ? [...a, [v.slice(2), all[i + 1]]] : a), []));
if (!args.from) throw new Error('--from <checkout with prototypes/landing and prototypes/river-page> is required');
const at = Date.parse(args.at ?? '2021-06-29T12:00:00Z') / 1000;
const DATA = path.join(args.from, 'prototypes/landing/public/data');
const EXTRA = path.join(args.from, 'prototypes/river-page/src/data');
const OUT = path.join(path.dirname(new URL(import.meta.url).pathname), '../dev/fixtures');
const HISTORY_S = 10 * 86400;
const read = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const maybe = (p) => (fs.existsSync(p) ? read(p) : null);

/** The part of a prototype series ({ t0, v } hourly, or with step_h) from `from` to `to` (Unix s), as a contract Series. */
function slice(s, from, to) {
  const step = s.step_h ?? 1;
  const k0 = Math.max(0, Math.ceil((from - s.t0) / (step * 3600)));
  const k1 = Math.min(s.v.length - 1, Math.floor((to - s.t0) / (step * 3600)));
  return { t0: s.t0 + k0 * step * 3600, step_h: step, v: s.v.slice(k0, k1 + 1) };
}

const localDate = (s) => new Date(s * 1000).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });

function bundle(id) {
  const dir = path.join(DATA, 'sites', id);
  const [meta, geo, clim, hc, obs, temp] = ['meta', 'geo', 'climatology', 'hindcast', 'observed', 'temp'].map((f) => read(path.join(dir, `${f}.json`)));
  const watershed = maybe(path.join(EXTRA, `basin-${id}.json`));
  const rating = maybe(path.join(EXTRA, `rating-${id}.json`));
  const idx = hc.issues.indexOf(at);
  if (idx < 0) throw new Error(`${id}: no flow issue at ${new Date(at * 1000).toISOString()}`);
  const nL = hc.leads.length;
  const nQ = hc.quants.length;
  const binH = hc.precip.bin_h;
  const nBins = 168 / binH;
  const nF = hc.precip.fields.length;
  const bin = (k, field) => hc.precip.bins[(idx * nBins + k) * nF + hc.precip.fields.indexOf(field)] ?? 0;
  const swe = (s) => obs.swe.v[Math.floor((s - obs.swe.t0) / (obs.swe.step_h * 3600))] ?? null;
  const melt = Array.from({ length: nBins }, (_, k) => {
    const mid = at + (k + 0.5) * binH * 3600;
    const a = swe(mid);
    const b = swe(mid + 86400);
    return a != null && b != null ? Math.max(0, a - b) * (binH / 24) : 0;
  });

  const run = temp.issues.indexOf(at);
  const tL = temp.leads.length;
  const nD = temp.highs.length / (temp.issues.length * temp.quants.length);
  const water_temp_forecast =
    run < 0
      ? null
      : {
          issued_at: at,
          leads_h: temp.leads,
          quantiles: temp.quants,
          temp_c: temp.hourly.slice(run * tL * temp.quants.length, (run + 1) * tL * temp.quants.length),
          daily_high: {
            dates: Array.from({ length: nD }, (_, d) => localDate(at + d * 86400)),
            temp_c: temp.highs.slice(run * nD * temp.quants.length, (run + 1) * nD * temp.quants.length),
          },
        };

  return {
    schema: 'flowcast.site.v0',
    generated_at: at,
    site: {
      ...summary(meta),
      area_mi2: meta.area_mi2,
      forest_frac: meta.forest_frac,
      snow_frac: meta.snow_frac,
      median_flow_cfs: meta.median_flow_cfs,
      travel_time_max_h: meta.travel_time_max_h,
      nid_dams: meta.nid_dams,
      nid_major_dams: meta.nid_major_dams,
      nws_lid: meta.nws_lid ?? null,
      flood_stage_ft: meta.flood_stage_ft ?? null,
      watershed: watershed && { level: watershed.level, huc: watershed.huc, name: watershed.name, parts: watershed.parts.map(({ huc, name }) => ({ huc, name })), source: watershed.source },
    },
    rating: rating && { rating_id: rating.rating_id, stage_ft: rating.stage_ft, flow_cfs: rating.flow_cfs },
    geo,
    climatology: { quantiles: [0.1, 0.25, 0.5, 0.75, 0.9], flow_cfs: clim.flow, water_temp_c: clim.temp, years: clim.years },
    observed: {
      flow_cfs: slice(obs.flow, at - HISTORY_S, at),
      water_temp_c: slice(obs.temp, at - HISTORY_S, at),
      stage_ft: null,
      swe_mm: slice(obs.swe, at - HISTORY_S, at),
      precip_mm: slice(obs.precip, at - HISTORY_S, at - 86400),
    },
    flow_forecast: {
      issued_at: at,
      leads_h: hc.leads,
      quantiles: hc.quants,
      flow_cfs: hc.flow.slice(idx * nL * nQ, (idx + 1) * nL * nQ),
      precip: {
        bin_h: binH,
        mean_mm: Array.from({ length: nBins }, (_, k) => bin(k, 'mean')),
        snow_share: Array.from({ length: nBins }, (_, k) => bin(k, 'snow_share')),
        melt_mm: melt.map((v) => Math.round(v * 100) / 100),
      },
    },
    water_temp_forecast,
  };
}

function summary(meta) {
  return { id: meta.id, name: meta.name, short: meta.short, river: meta.river, place: meta.place, lat: meta.lat, lon: meta.lon };
}

const ids = read(path.join(DATA, 'sites.json')).featured.map((s) => s.id);
fs.mkdirSync(path.join(OUT, 'sites'), { recursive: true });
const index = { schema: 'flowcast.sites.v0', default: '01427510', sites: [] };
for (const id of ids) {
  const b = bundle(id);
  index.sites.push(summary(b.site));
  const out = path.join(OUT, 'sites', `${id}.json`);
  fs.writeFileSync(out, JSON.stringify(b));
  console.log(`${out}: ${(fs.statSync(out).size / 1e3).toFixed(0)} kB`);
}
fs.writeFileSync(path.join(OUT, 'sites.json'), JSON.stringify(index, null, 1));
console.log(`${path.join(OUT, 'sites.json')}: ${index.sites.length} sites, default ${index.default}`);
