const MM_PER_IN = 25.4;
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const OPEN_METEO = 'https://api.open-meteo.com/v1/forecast';
const OPEN_METEO_ARCHIVE = 'https://archive-api.open-meteo.com/v1/archive';

// ---------------------------------------------------------------------------------------------- series

/** Value of a regular series (contract `Series`) at a time, or null. */
function seriesAt(s, ms) {
  if (!s) return null;
  const k = Math.round((ms - s.t0 * 1000) / (s.step_h * HOUR));
  return s.v[k] ?? null;
}

/** Value of the step containing a time (for daily totals and daily snapshots), or null. */
function stepAt(s, ms) {
  if (!s) return null;
  const k = Math.floor((ms - s.t0 * 1000) / (s.step_h * HOUR));
  return s.v[k] ?? null;
}

function nearestObserved(s, ms, withinH) {
  for (let d = 0; d <= withinH; d++) {
    const v = seriesAt(s, ms - d * HOUR) ?? seriesAt(s, ms + d * HOUR);
    if (v != null) return v;
  }
  return null;
}

/** The latest non-null reading of a series within a week of its end, as { t, v }. */
function latest(s) {
  if (!s) return null;
  for (let k = s.v.length - 1; k >= 0 && k >= s.v.length - (7 * 24) / s.step_h; k--) {
    if (s.v[k] != null) return { t: new Date((s.t0 + k * s.step_h * 3600) * 1000), v: s.v[k] };
  }
  return null;
}

const quants = (values, nQ, row) => values.slice(row * nQ, row * nQ + nQ);

// ---------------------------------------------------------------------------------------------- gauge

/** The gauge "now" from the bundle's observations: latest flow, stage (if reported), water temperature, and the past week of flow. */
export function gaugeNow(bundle) {
  const obs = bundle.observed;
  const flow = latest(obs.flow_cfs);
  const series = [];
  if (flow) {
    for (let ms = flow.t.getTime() - 7 * DAY; ms <= flow.t.getTime(); ms += HOUR) {
      const v = seriesAt(obs.flow_cfs, ms);
      if (v != null) series.push({ t: new Date(ms), v });
    }
  }
  return { flow, stage: latest(obs.stage_ft), temp: latest(obs.water_temp_c), series };
}

// ---------------------------------------------------------------------------------------------- flow forecast

/** Flow quantiles, GEFS water input and observed context for the bundle's latest flow forecast. */
export function flowForecast(bundle) {
  const fc = bundle.flow_forecast;
  const obs = bundle.observed;
  const issue = fc.issued_at * 1000;
  const nQ = fc.quantiles.length;
  const fan = [{ t: new Date(issue), q05: null, q25: null, q50: null, q75: null, q95: null }];
  fc.leads_h.forEach((lead, l) => {
    const [q05, q25, q50, q75, q95] = quants(fc.flow_cfs, nQ, l);
    fan.push({ t: new Date(issue + lead * HOUR), q05, q25, q50, q75, q95 });
  });
  const now = nearestObserved(obs.flow_cfs, issue, 3);
  if (now != null) Object.assign(fan[0], { q05: now, q25: now, q50: now, q75: now, q95: now });
  else fan.shift();

  const binH = fc.precip.bin_h;
  const water = fc.precip.mean_mm.map((mean, k) => {
    const t0 = issue + k * binH * HOUR;
    const share = fc.precip.snow_share[k] ?? 0;
    return {
      t0: new Date(t0),
      t1: new Date(t0 + binH * HOUR),
      rain: ((mean ?? 0) * (1 - share)) / MM_PER_IN,
      snow: ((mean ?? 0) * share) / MM_PER_IN,
      melt: (fc.precip.melt_mm?.[k] ?? 0) / MM_PER_IN,
    };
  });

  const from = issue - 3 * DAY;
  const to = issue + 7 * DAY;
  const pastRain = [];
  for (let t0 = from; t0 < issue; t0 += binH * HOUR) {
    const daily = stepAt(obs.precip_mm, t0 + (binH / 2) * HOUR);
    if (daily != null) pastRain.push({ t0: new Date(t0), t1: new Date(t0 + binH * HOUR), rain: (daily * (binH / obs.precip_mm.step_h)) / MM_PER_IN });
  }
  const observed = [];
  for (let t = from; t <= to; t += HOUR) {
    const v = seriesAt(obs.flow_cfs, t);
    if (v != null) observed.push({ t: new Date(t), v, after: t > issue });
  }

  for (const r of fan) r.obs = seriesAt(obs.flow_cfs, r.t.getTime());
  const ahead = fan.filter((r) => r.t.getTime() > issue);
  const peak = ahead.reduce((m, r) => (r.q50 > m.q50 ? r : m), ahead[0]);
  const swe0 = stepAt(obs.swe_mm, issue);
  return {
    issue: new Date(issue),
    from: new Date(from),
    to: new Date(to),
    fan,
    water,
    pastRain,
    observed,
    now,
    peak,
    sweIn: swe0 == null ? null : swe0 / MM_PER_IN,
    totals: {
      rain: water.reduce((s, w) => s + w.rain, 0),
      snow: water.reduce((s, w) => s + w.snow, 0),
      melt: water.reduce((s, w) => s + w.melt, 0),
    },
  };
}

// ---------------------------------------------------------------------------------------------- water temperature

const toF = (c) => (c == null ? null : (c * 9) / 5 + 32);

function nyMidnight(ms) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' })
      .formatToParts(new Date(ms))
      .map((p) => [p.type, p.value]),
  );
  return ms - (Number(parts.hour) * 60 + Number(parts.minute)) * 60000;
}

/**
 * Water-temperature forecast in °F from `issue` (the flow forecast's issue time, so both charts share a "now"):
 * the hourly fan for the next 7 days from the bundle's latest temperature run, the model's daily highs in that
 * window (not drawn; they tell a warming week from a cooling one), and observed context.
 */
export function tempForecast(bundle, issueDate) {
  const tc = bundle.water_temp_forecast;
  const obs = bundle.observed.water_temp_c;
  const clim = bundle.climatology.water_temp_c;
  const issue = issueDate.getTime();
  const end = issue + 7 * DAY;
  const fan = [];
  const now = nearestObserved(obs, issue, 3);
  if (now != null) fan.push({ t: new Date(issue), q05: toF(now), q25: toF(now), q50: toF(now), q75: toF(now), q95: toF(now) });
  const highs = [];
  if (tc) {
    const t0 = tc.issued_at * 1000;
    const nQ = tc.quantiles.length;
    tc.leads_h.forEach((lead, l) => {
      const t = t0 + lead * HOUR;
      const [q05, q25, q50, q75, q95] = quants(tc.temp_c, nQ, l).map(toF);
      if (q50 != null && t > issue && t <= end) fan.push({ t: new Date(t), q05, q25, q50, q75, q95, obs: toF(seriesAt(obs, t)) });
    });
    tc.daily_high.dates.forEach((date, d) => {
      const [q05, q25, q50, q75, q95] = quants(tc.daily_high.temp_c, nQ, d).map(toF);
      const [y, m, dd] = date.split('-').map(Number);
      const afternoon = nyMidnight(Date.UTC(y, m - 1, dd, 16)) + 15 * HOUR;
      if (q50 != null && afternoon > issue && afternoon < end) highs.push({ t: new Date(afternoon), q05, q25, q50, q75, q95 });
    });
  }

  const from = issue - 3 * DAY;
  const observed = [];
  for (let t = from; t <= end; t += HOUR) {
    const v = seriesAt(obs, t);
    if (v != null) observed.push({ t: new Date(t), v: toF(v), after: t > issue });
  }
  const normal = [];
  for (let t = from; t <= end; t += 6 * HOUR) {
    const d = new Date(t);
    const k = Math.min(366, Math.max(1, dayOfYear(d))) - 1;
    const [, p25, , p75] = clim.slice(k * 5, k * 5 + 5);
    normal.push({ t: d, lo: toF(p25), hi: toF(p75) });
  }
  return { issue: new Date(issue), from: new Date(from), to: new Date(end), fan, highs, observed, normal, now: toF(now) };
}

/**
 * Points sorted by `t` with a null `v` inserted wherever consecutive readings are more than `maxGapH` apart,
 * so a line bridges the record's routine missing hours but still breaks at real outages.
 */
export function withGaps(points, maxGapH = 6) {
  const out = [];
  for (const p of points) {
    const prev = out.at(-1);
    if (prev && p.t - prev.t > maxGapH * HOUR) out.push({ t: new Date(prev.t.getTime() + HOUR), v: null });
    out.push(p);
  }
  return out;
}

// ---------------------------------------------------------------------------------------------- climatology

export function dayOfYear(d) {
  const start = Date.UTC(d.getFullYear(), 0, 0);
  return Math.floor((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - start) / DAY);
}

/** 10/25/50/75/90th percentiles of daily mean flow for a date, from the bundle's `climatology`. */
export function normalFor(clim, d) {
  const k = Math.min(366, Math.max(1, dayOfYear(d))) - 1;
  const [p10, p25, p50, p75, p90] = clim.flow_cfs.slice(k * 5, k * 5 + 5);
  return { p10, p25, p50, p75, p90 };
}

export function normalBand(clim, from, to) {
  const out = [];
  for (let t = from.getTime(); t <= to.getTime(); t += 6 * HOUR) out.push({ t: new Date(t), ...normalFor(clim, new Date(t)) });
  return out;
}

export function flowClass(clim, d, v) {
  const n = normalFor(clim, d);
  if (v == null || n.p50 == null) return null;
  if (v < n.p10) return { label: 'Much below normal', tone: 'low' };
  if (v < n.p25) return { label: 'Below normal', tone: 'low' };
  if (v <= n.p75) return { label: 'Normal', tone: 'normal' };
  if (v <= n.p90) return { label: 'Above normal', tone: 'high' };
  return { label: 'Much above normal', tone: 'high' };
}

// ---------------------------------------------------------------------------------------------- weather grid

export function basinGrid(bounds, step = 0.1) {
  const [x0, y0, x1, y1] = bounds;
  const nx = Math.ceil((x1 - x0) / step) + 1;
  const ny = Math.ceil((y1 - y0) / step) + 1;
  const pts = [];
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) pts.push([x0 + i * step, y0 + j * step]);
  return { nx, ny, step, x0, y0, pts };
}

const WEATHER_TTL = 30 * 60 * 1000;
const inflight = new Map();

const nyDate = (ms) => new Date(ms).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });

/**
 * Weather at the grid points that touch the basin (Open-Meteo, keyless): live when `at` is null, otherwise
 * the ERA5 archive around `at`, where "next 3 days" is what actually fell. Open-Meteo rate-limits per
 * location, so only points inside the basin or one step from it are requested, and results are cached for
 * 30 minutes and shared between concurrent callers.
 */
export function basinWeather(grid, near, at = null) {
  const want = grid.pts.map((p) => near(p));
  const pts = grid.pts.filter((_, i) => want[i]);
  const where = `latitude=${pts.map((p) => p[1].toFixed(3)).join(',')}&longitude=${pts.map((p) => p[0].toFixed(3)).join(',')}`;
  const url = at
    ? `${OPEN_METEO_ARCHIVE}?${where}&hourly=precipitation,temperature_2m,snow_depth&daily=shortwave_radiation_sum` +
      `&start_date=${nyDate(at.getTime() - DAY)}&end_date=${nyDate(at.getTime() + 3 * DAY)}&timezone=America%2FNew_York`
    : `${OPEN_METEO}?${where}&current=temperature_2m,snow_depth&daily=precipitation_sum,shortwave_radiation_sum` +
      '&past_days=1&forecast_days=4&timezone=America%2FNew_York';
  if (!inflight.has(url)) inflight.set(url, fetchCached(url).finally(() => inflight.delete(url)));
  return inflight.get(url).then((rows) => {
    const list = Array.isArray(rows) ? rows : [rows];
    const spread = (f) => {
      let k = 0;
      return want.map((w) => (w ? f(list[k++]) : null));
    };
    const sum = (a, i, j) => a.slice(Math.max(0, i), j).reduce((s, v) => s + (v ?? 0), 0);
    if (!at) {
      return {
        time: list[0]?.current?.time,
        fields: {
          rain24: spread((r) => r.daily.precipitation_sum[0] / MM_PER_IN),
          rainNext: spread((r) => sum(r.daily.precipitation_sum, 1, 4) / MM_PER_IN),
          snowDepth: spread((r) => ((r.current.snow_depth ?? 0) * 1000) / MM_PER_IN),
          sun: spread((r) => r.daily.shortwave_radiation_sum[1]),
          airTemp: spread((r) => (r.current.temperature_2m * 9) / 5 + 32),
        },
      };
    }
    // Hourly arrays start at local midnight of start_date; utc_offset_seconds locates `at` in them.
    const r0 = list[0];
    const [y, m, d] = r0.hourly.time[0].slice(0, 10).split('-').map(Number);
    const k = Math.round((at.getTime() - (Date.UTC(y, m - 1, d) - r0.utc_offset_seconds * 1000)) / HOUR);
    return {
      time: null,
      fields: {
        rain24: spread((r) => sum(r.hourly.precipitation, k - 24, k) / MM_PER_IN),
        rainNext: spread((r) => sum(r.hourly.precipitation, k, k + 72) / MM_PER_IN),
        snowDepth: spread((r) => ((r.hourly.snow_depth[k] ?? 0) * 1000) / MM_PER_IN),
        sun: spread((r) => r.daily.shortwave_radiation_sum[1]),
        airTemp: spread((r) => (r.hourly.temperature_2m[k] * 9) / 5 + 32),
      },
    };
  });
}

async function fetchCached(url) {
  let h = 0;
  for (let i = 0; i < url.length; i++) h = (h * 31 + url.charCodeAt(i)) | 0;
  const key = `wx:${h}`;
  try {
    const hit = JSON.parse(localStorage.getItem(key));
    if (hit && Date.now() - hit.at < WEATHER_TTL) return hit.rows;
  } catch {
    // corrupt cache entry, refetch
  }
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Open-Meteo ${r.status}`);
  const rows = await r.json();
  localStorage.setItem(key, JSON.stringify({ at: Date.now(), rows }));
  return rows;
}

// ---------------------------------------------------------------------------------------------- formatting

export const fmt = {
  cfs: (v) => (v == null ? '—' : (v >= 1000 ? Number(v.toPrecision(3)) : Math.round(v)).toLocaleString('en-US')),
  int: (v) => (v == null ? '—' : Math.round(v).toLocaleString('en-US')),
  in: (v, d = 1) => (v == null ? '—' : `${v < 0.05 && v > 0 ? '<0.1' : v.toFixed(d)} in`),
  pct: (v) => `${Math.round(v * 100)}%`,
  f: (c) => (c == null ? '—' : `${Math.round((c * 9) / 5 + 32)}°F`),
  ft: (m) => Math.round(m * 3.28084).toLocaleString('en-US'),
  when: (d) =>
    d.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }),
  day: (d) => d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'America/New_York' }),
  whenYear: (d) =>
    d.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }),
  monthDay: (d) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'America/New_York' }),
  date: (d) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'America/New_York' }),
};

