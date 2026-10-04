// Writes dev fixtures in the serving contract v1 (serving/schema/ on PR #64, plus the fields web/CONTRACT.md
// proposes) from the river-page prototype's data, as if each site's forecast had just been issued at `--at`.
// Dev only: `npm run dev` serves them through dev/mock-api.js in place of the backend.
//
//   node scripts/make-fixtures.mjs --from <flowcast checkout on cursor/simple-site-page-4752> [--at 2021-06-29T12:00:00Z]
//
// Stand-ins where the prototype's data differs from production: flow leads past 48 h are interpolated in time
// between the stored 12-hourly leads (v1 is hourly to 168 h); snowmelt is the observed snowpack's decline over
// the week (production has a SNOW-17 estimate); past rain is the AORC daily total spread over its 6 h bins.
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => (v.startsWith('--') ? [...a, [v.slice(2), all[i + 1]]] : a), []));
if (!args.from) throw new Error('--from <checkout with prototypes/landing and prototypes/river-page> is required');
const at = Date.parse(args.at ?? '2021-06-29T12:00:00Z') / 1000;
const DATA = path.join(args.from, 'prototypes/landing/public/data');
const EXTRA = path.join(args.from, 'prototypes/river-page/src/data');
const OUT = path.join(path.dirname(new URL(import.meta.url).pathname), '../dev/fixtures/data/v1');
const H = 3600;
const iso = (s) => new Date(s * 1000).toISOString().replace('.000Z', 'Z');
const issueKey = (s) => iso(s).slice(0, 13).replace(/[-T]/g, '');
const localDate = (s) => new Date(s * 1000).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
const read = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const maybe = (p) => (fs.existsSync(p) ? read(p) : null);
const round = (v, d = 2) => (v == null ? null : Math.round(v * 10 ** d) / 10 ** d);
const write = (p, obj) => {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj));
  console.log(`${path.relative(process.cwd(), p)}: ${(fs.statSync(p).size / 1e3).toFixed(0)} kB`);
};

/** Hourly prototype series ({ t0, v }) from `from` to `to` (Unix s) as a contract Series. */
function hourly(s, from, to, unit) {
  const k0 = Math.max(0, Math.ceil((from - s.t0) / H));
  const k1 = Math.min(s.v.length - 1, Math.floor((to - s.t0) / H));
  return { unit, start: iso(s.t0 + k0 * H), step_h: 1, values: s.v.slice(k0, k1 + 1) };
}

function interp(xs, ys, x) {
  let i = xs.findIndex((v) => v >= x);
  if (i <= 0) return i === 0 ? ys[0] : ys.at(-1);
  const a = ys[i - 1];
  const b = ys[i];
  return a == null || b == null ? null : a + ((b - a) * (x - xs[i - 1])) / (xs[i] - xs[i - 1]);
}

function site(id) {
  const dir = path.join(DATA, 'sites', id);
  const [meta, geo, clim, hc, obs, temp] = ['meta', 'geo', 'climatology', 'hindcast', 'observed', 'temp'].map((f) => read(path.join(dir, `${f}.json`)));
  const watershed = maybe(path.join(EXTRA, `basin-${id}.json`));
  const rating = maybe(path.join(EXTRA, `rating-${id}.json`));
  const idx = hc.issues.indexOf(at);
  if (idx < 0) throw new Error(`${id}: no flow issue at ${iso(at)}`);
  const sid = `USGS-${id}`;
  const key = issueKey(at);
  const quant = ['q05', 'q25', 'q50', 'q75', 'q95'];

  const nL = hc.leads.length;
  const nQ = hc.quants.length;
  const flow = { unit: 'ft3/s', start: iso(at + H), step_h: 1, calibrated: true, n_samples: 132 };
  quant.forEach((q, j) => {
    const ys = hc.leads.map((_, l) => hc.flow[(idx * nL + l) * nQ + j]);
    flow[q] = Array.from({ length: 168 }, (_, i) => round(interp(hc.leads, ys, i + 1), 1));
  });

  const run = temp.issues.indexOf(at);
  const tQ = temp.quants.length;
  const tL = temp.leads.length;
  const nD = temp.highs.length / (temp.issues.length * tQ);
  const temperature =
    run < 0
      ? null
      : {
          hourly: Object.fromEntries([
            ['unit', 'degC'],
            ['start', iso(at + H)],
            ['step_h', 1],
            ...quant.map((q, j) => [q, temp.leads.map((_, l) => temp.hourly[(run * tL + l) * tQ + j])]),
          ]),
          daily_max: Array.from({ length: nD }, (_, d) => {
            const v = quant.map((_, j) => temp.highs[(run * nD + d) * tQ + j]);
            return v[2] == null ? null : { date: localDate(at + d * 86400), lead_day: d, ...Object.fromEntries(quant.map((q, j) => [q, v[j]])) };
          }).filter(Boolean),
          calibrated: false,
          n_samples: 88,
        };

  const binH = hc.precip.bin_h;
  const nBins = 168 / binH;
  const nF = hc.precip.fields.length;
  const field = (k, f) => hc.precip.bins[(idx * nBins + k) * nF + hc.precip.fields.indexOf(f)] ?? 0;
  const swe = (s) => obs.swe.v[Math.floor((s - obs.swe.t0) / (obs.swe.step_h * H))] ?? null;
  const bins = { start: iso(at + binH * H), step_h: binH, rain_mm: [], snow_mm: [], snowmelt_mm: [] };
  for (let k = 0; k < nBins; k++) {
    const mean = field(k, 'mean');
    const share = field(k, 'snow_share');
    const mid = at + (k + 0.5) * binH * H;
    const a = swe(mid);
    const b = swe(mid + 86400);
    bins.rain_mm.push(round(mean * (1 - share)));
    bins.snow_mm.push(round(mean * share));
    bins.snowmelt_mm.push(round(a != null && b != null ? Math.max(0, a - b) * (binH / 24) : 0));
  }
  const pastStart = at - 72 * H + binH * H;
  const past_rain = {
    unit: 'mm',
    start: iso(pastStart),
    step_h: binH,
    values: Array.from({ length: 72 / binH }, (_, k) => {
      const end = pastStart + k * binH * H;
      const daily = obs.precip.v[Math.floor((end - binH * H - obs.precip.t0) / (obs.precip.step_h * H))];
      return daily == null ? null : round((daily * binH) / 24);
    }),
  };

  const forecast = {
    schema: 'flowcast.forecast/v1',
    id: sid,
    slug: meta.slug,
    issue: key,
    issue_time: iso(at),
    created: iso(at + 1800),
    run_type: 'operational',
    trigger: 'cycle',
    models: { flow: 'fixture', temp: temperature ? 'fixture' : null, snow: null },
    flow,
    temperature,
    weather: { gefs_init: iso(at - 12 * H), members: 11, bins, past_rain, snowpack_swe_mm: swe(at) },
  };

  const pointer = { issue: key, issue_time: iso(at), url: `/data/v1/sites/${sid}/forecasts/${key}.json`, age_h: 0.5 };
  const obsFrom = at - 30 * 86400;
  const flowAt = (s) => obs.flow.v[Math.round((s - obs.flow.t0) / H)] ?? null;
  const live = {
    schema: 'flowcast.live/v1',
    id: sid,
    slug: meta.slug,
    generated: iso(at + 1800),
    status: 'active',
    always_on: true,
    always_on_reasons: ['pinned'],
    awake_until: null,
    forecast: pointer,
    recent_forecasts: [pointer],
    static_url: `/data/v1/sites/${sid}/static.json`,
    now: {
      observed_at: iso(at),
      flow_cfs: flowAt(at),
      flow_change_24h_cfs: flowAt(at) != null && flowAt(at - 86400) != null ? round(flowAt(at) - flowAt(at - 86400), 0) : null,
      stage_ft: rating && flowAt(at) != null ? round(interp(rating.flow_cfs, rating.stage_ft, flowAt(at))) : null,
      water_temp_c: obs.temp.v[Math.round((at - obs.temp.t0) / H)] ?? null,
      gauge_stale: false,
      flood_category: null,
    },
    observations: { discharge: hourly(obs.flow, obsFrom, at, 'ft3/s'), water_temperature: hourly(obs.temp, obsFrom, at, 'degC') },
  };

  const stageToFlow = (ft) => (rating ? interp(rating.stage_ft, rating.flow_cfs, ft) : null);
  const stat = {
    schema: 'flowcast.static/v1',
    id: sid,
    slug: meta.slug,
    name: meta.name,
    short_name: meta.short,
    river: meta.river,
    lat: meta.lat,
    lon: meta.lon,
    timezone: 'America/New_York',
    has_temperature: !!temperature,
    nws_lid: meta.nws_lid ?? null,
    usgs_url: `https://waterdata.usgs.gov/monitoring-location/${sid}/`,
    basin: {
      area_km2: meta.area_km2,
      area_sq_mi: meta.area_mi2,
      elevation_m: meta.elevation_m,
      forest_frac: meta.forest_frac,
      developed_frac: meta.developed_frac,
      frac_snow: meta.snow_frac,
      below_dam: meta.below_dam,
      n_major_dams: meta.nid_major_dams,
      n_dams: meta.nid_dams,
      travel_time_max_h: meta.travel_time_max_h,
      median_flow_cfs: meta.median_flow_cfs,
    },
    flood_categories: Object.entries(meta.flood_stage_ft ?? {}).map(([category, stage_ft]) => ({ category, stage_ft, flow_cfs: round(stageToFlow(stage_ft), 0) })),
    geometry: { bounds: geo.bounds, basin: geo.basin, rivers: geo.rivers, gauges: geo.gauges, dams: geo.dams },
    watershed_description: null,
    watershed: watershed && { level: watershed.level, huc: watershed.huc, name: watershed.name, parts: watershed.parts.map(({ huc, name }) => ({ huc, name })), source: watershed.source },
    climatology: { quantiles: [0.1, 0.25, 0.5, 0.75, 0.9], flow_cfs: clim.flow, water_temp_c: clim.temp, years: clim.years },
  };

  const summary = {
    id: sid,
    slug: meta.slug,
    name: meta.name,
    river: meta.river,
    town: meta.place.split(', ')[0],
    state: meta.place.split(', ').at(-1),
    lat: meta.lat,
    lon: meta.lon,
    area_mi2: Math.round(meta.area_mi2),
    has_temperature: !!temperature,
    forecast_ready: true,
    forecast_issued_at: iso(at),
    status: 'active',
    always_on: true,
    live_url: `/data/v1/sites/${sid}/live.json`,
  };
  return { sid, key, live, stat, forecast, summary };
}

const ids = read(path.join(DATA, 'sites.json')).featured.map((s) => s.id);
fs.rmSync(OUT, { recursive: true, force: true });
const sites = ids.map(site);
for (const s of sites) {
  write(path.join(OUT, 'sites', s.sid, 'live.json'), s.live);
  write(path.join(OUT, 'sites', s.sid, 'static.json'), s.stat);
  write(path.join(OUT, 'sites', s.sid, 'forecasts', `${s.key}.json`), s.forecast);
}
// One site the model covers but doesn't serve, to exercise that page.
const notReady = { id: 'USGS-01194000', slug: null, name: 'Eightmile River at North Plain, CT', river: 'Eightmile River', town: 'North Plain', state: 'CT', lat: 41.44177, lon: -72.33286, area_mi2: 20.1, has_temperature: true, forecast_ready: false, forecast_issued_at: null, status: null, always_on: false, live_url: null };
write(path.join(OUT, 'sites.json'), { schema: 'flowcast.sites/v1', generated: iso(at + 1800), default: 'USGS-01427510', sites: [...sites.map((s) => s.summary), notReady] });
