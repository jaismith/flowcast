const MM_PER_IN = 25.4;
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const OPEN_METEO = 'https://api.open-meteo.com/v1/forecast';
const OPEN_METEO_ARCHIVE = 'https://archive-api.open-meteo.com/v1/archive';

// Model forecasts may only come from the validation years. Anything outside this window is refused.
export const VALIDATION = { start: Date.UTC(2020, 9, 1), end: Date.UTC(2022, 9, 1), label: 'WY2021–2022' };

export async function loadJSON(path) {
  const r = await fetch(`${import.meta.env.BASE_URL}data/${path}`);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

export async function loadSite(id) {
  const [meta, geo, clim, hindcast, observed, temp] = await Promise.all(
    ['meta', 'geo', 'climatology', 'hindcast', 'observed', 'temp'].map((f) => loadJSON(`sites/${id}/${f}.json`)),
  );
  for (const t of [hindcast.issues[0], hindcast.issues.at(-1), temp.issues[0], temp.issues.at(-1)]) {
    if (t * 1000 < VALIDATION.start || t * 1000 >= VALIDATION.end) throw new Error('hindcast outside validation years');
  }
  return { meta, geo, clim, hindcast, observed, temp };
}

// ---------------------------------------------------------------------------------------------- replay

/** Indices of the regular 00/12 UTC issues (the hindcast file also carries off-cycle MARFC bulletin times). */
export function replayIssues(hc) {
  return hc.issues.map((_, i) => i).filter((i) => !hc.is_marfc_time[i]);
}

function nearestIssue(hc, list, ms) {
  let best = list[0];
  for (const i of list) if (Math.abs(hc.issues[i] * 1000 - ms) < Math.abs(hc.issues[best] * 1000 - ms)) best = i;
  return best;
}

/** Today's calendar date replayed in the latest validation year that has a full 7-day window, at 12 UTC. */
export function sameDateIssue(hc, list, now = new Date()) {
  for (const y of [2022, 2021, 2020]) {
    const ms = Date.UTC(y, now.getMonth(), now.getDate(), 12);
    if (ms >= VALIDATION.start && ms + 7 * DAY < VALIDATION.end) return { idx: nearestIssue(hc, list, ms), year: y };
  }
  return { idx: list[0], year: 2021 };
}

export function presets(hc, list) {
  const out = [];
  const today = sameDateIssue(hc, list);
  out.push({ key: 'today', label: `Today's date, ${today.year}`, idx: today.idx });
  const seen = new Set([today.idx]);
  const sorted = [...hc.events].filter((e) => e.kind === 'flood' || e.kind === 'melt').sort((a, b) => a.time - b.time);
  for (const e of sorted) {
    const idx = nearestIssue(hc, list, hc.issues[e.issue] * 1000);
    const month = new Date(e.time * 1000).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
    const dup = [...seen].some((s) => Math.abs(hc.issues[s] - hc.issues[idx]) < 4 * 86400);
    if (dup) continue;
    seen.add(idx);
    out.push({ key: `${e.kind}-${e.time}`, label: `${e.kind === 'melt' ? 'Snowmelt' : 'Flood'} · ${month}`, idx });
  }
  return out;
}

/** Forecast quantiles, GEFS water input and observed context for one issue. */
export function forecastAt(data, idx) {
  const { hindcast: hc, observed: obs } = data;
  const issue = hc.issues[idx] * 1000;
  const nQ = hc.quants.length;
  const nL = hc.leads.length;
  const fan = [{ t: new Date(issue), q05: null, q25: null, q50: null, q75: null, q95: null }];
  for (let l = 0; l < nL; l++) {
    const base = (idx * nL + l) * nQ;
    const [q05, q25, q50, q75, q95] = hc.flow.slice(base, base + nQ);
    fan.push({ t: new Date(issue + hc.leads[l] * HOUR), q05, q25, q50, q75, q95 });
  }
  const now = nearestObserved(obs.flow, issue, 3);
  if (now != null) Object.assign(fan[0], { q05: now, q25: now, q50: now, q75: now, q95: now });
  else fan.shift();

  const binH = hc.precip.bin_h;
  const nBins = 168 / binH;
  const nF = hc.precip.fields.length;
  const sweDay = (ms) => {
    const k = Math.floor((ms - obs.swe.t0 * 1000) / (obs.swe.step_h * HOUR));
    return obs.swe.v[k] ?? null;
  };
  const water = [];
  for (let k = 0; k < nBins; k++) {
    const base = (idx * nBins + k) * nF;
    const mean = hc.precip.bins[base] ?? 0;
    const share = hc.precip.bins[base + 2] ?? 0;
    const t0 = issue + k * binH * HOUR;
    const mid = t0 + (binH / 2) * HOUR;
    const a = sweDay(mid);
    const b = sweDay(mid + DAY);
    const meltDay = a != null && b != null ? Math.max(0, a - b) : 0;
    water.push({
      t0: new Date(t0),
      t1: new Date(t0 + binH * HOUR),
      rain: (mean * (1 - share)) / MM_PER_IN,
      snow: (mean * share) / MM_PER_IN,
      melt: (meltDay * (binH / 24)) / MM_PER_IN,
    });
  }

  const from = issue - 3 * DAY;
  const to = issue + 7 * DAY;
  const pastRain = [];
  for (let t0 = from; t0 < issue; t0 += binH * HOUR) {
    const k = Math.floor((t0 + (binH / 2) * HOUR - obs.precip.t0 * 1000) / (obs.precip.step_h * HOUR));
    const daily = obs.precip.v[k];
    if (daily != null) pastRain.push({ t0: new Date(t0), t1: new Date(t0 + binH * HOUR), rain: (daily * (binH / 24)) / MM_PER_IN });
  }
  const observed = [];
  for (let t = from; t <= to; t += HOUR) {
    const v = observedAt(obs.flow, t);
    if (v != null) observed.push({ t: new Date(t), v, after: t > issue });
  }

  for (const r of fan) r.obs = observedAt(obs.flow, r.t.getTime());
  const ahead = fan.filter((r) => r.t.getTime() > issue);
  const peak = ahead.reduce((m, r) => (r.q50 > m.q50 ? r : m), ahead[0]);
  const obsAfter = observed.filter((r) => r.after && r.v != null);
  const obsPeak = obsAfter.reduce((m, r) => (r.v > m.v ? r : m), obsAfter[0] ?? { v: null });
  const swe0 = sweDay(issue);
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
    obsPeak,
    sweIn: swe0 == null ? null : swe0 / MM_PER_IN,
    totals: {
      rain: water.reduce((s, w) => s + w.rain, 0),
      snow: water.reduce((s, w) => s + w.snow, 0),
      melt: water.reduce((s, w) => s + w.melt, 0),
    },
  };
}

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
 * Water-temperature forecast for one issue, in °F, from the latest morning (12 UTC) temperature run at or before
 * the issue (there are only morning runs): the hourly fan for the next 7 days, the model's daily highs for the days
 * in that window (not drawn; they tell a warming week from a cooling one), and observed context.
 */
export function tempForecastAt(data, idx) {
  const { hindcast: hc, observed: obs, clim, temp: tc } = data;
  const issue = hc.issues[idx] * 1000;
  const nL = tc.leads.length;
  const nQ = tc.quants.length;
  const nD = tc.highs.length / (tc.issues.length * nQ);
  const q = (arr, base) => arr.slice(base, base + nQ).map(toF);

  const fan = [];
  const now = nearestObserved(obs.temp, issue, 3);
  if (now != null) fan.push({ t: new Date(issue), q05: toF(now), q25: toF(now), q50: toF(now), q75: toF(now), q95: toF(now) });
  let j = -1;
  for (let k = 0; k < tc.issues.length && tc.issues[k] * 1000 <= issue; k++) j = k;
  const run = j >= 0 && issue - tc.issues[j] * 1000 < DAY ? j : -1;
  const highs = [];
  if (run >= 0) {
    const t0 = tc.issues[run] * 1000;
    const hourly = [];
    for (let l = 0; l < nL; l++) {
      const t = t0 + tc.leads[l] * HOUR;
      const [q05, q25, q50, q75, q95] = q(tc.hourly, (run * nL + l) * nQ);
      if (q50 != null) hourly.push({ t: new Date(t), q05, q25, q50, q75, q95, obs: toF(observedAt(obs.temp, t)) });
    }
    const end = issue + 7 * DAY;
    fan.push(...hourly.filter((r) => r.t > issue && r.t <= end));
    for (let d = 0; d < nD; d++) {
      const [q05, q25, q50, q75, q95] = q(tc.highs, (run * nD + d) * nQ);
      const afternoon = nyMidnight(t0 + d * DAY) + 15 * HOUR;
      if (q50 != null && afternoon > issue && afternoon < end) highs.push({ t: new Date(afternoon), q05, q25, q50, q75, q95 });
    }
  }

  const from = issue - 3 * DAY;
  const to = issue + 7 * DAY;
  const observed = [];
  for (let t = from; t <= to; t += HOUR) {
    const v = observedAt(obs.temp, t);
    if (v != null) observed.push({ t: new Date(t), v: toF(v), after: t > issue });
  }
  const normal = [];
  for (let t = from; t <= to; t += 6 * HOUR) {
    const d = new Date(t);
    const k = Math.min(366, Math.max(1, dayOfYear(d))) - 1;
    const [, p25, , p75] = clim.temp.slice(k * 5, k * 5 + 5);
    normal.push({ t: d, lo: toF(p25), hi: toF(p75) });
  }
  return { issue: new Date(issue), from: new Date(from), to: new Date(to), fan, highs, observed, normal, now: toF(now) };
}

function observedAt(series, ms) {
  const k = Math.round((ms - series.t0 * 1000) / HOUR);
  return series.v[k] ?? null;
}

function nearestObserved(series, ms, withinH) {
  for (let d = 0; d <= withinH; d++) {
    const v = observedAt(series, ms - d * HOUR) ?? observedAt(series, ms + d * HOUR);
    if (v != null) return v;
  }
  return null;
}

/**
 * Points sorted by `t` with a null `v` inserted wherever consecutive readings are more than `maxGapH` apart,
 * so a line bridges the archive's routine missing hours but still breaks at real outages.
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

/** 10/25/50/75/90th percentiles of daily mean flow for a date (2000–2022). */
export function normalFor(clim, d) {
  const k = Math.min(366, Math.max(1, dayOfYear(d))) - 1;
  const [p10, p25, p50, p75, p90] = clim.flow.slice(k * 5, k * 5 + 5);
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

// ---------------------------------------------------------------------------------------------- live readings

/**
 * Latest readings and the past week of hourly flow from the site's live.json (the backend refreshes it hourly; the
 * page never calls USGS). Water temperature is °C. Rejects when the site has no live.json yet.
 */
export async function liveGauge(id) {
  const r = await fetch(`/data/v1/sites/USGS-${id}/live.json`, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`live.json ${r.status}`);
  const live = await r.json();
  const { now, observations: obs } = live;
  const at = now.observed_at ? new Date(now.observed_at) : null;
  const reading = (v) => (at && v != null ? { t: at, v } : null);
  const series = [];
  const q = obs?.discharge;
  if (q) {
    const t0 = Date.parse(q.start);
    const from = (at ?? new Date()).getTime() - 7 * DAY;
    q.values.forEach((v, i) => {
      const t = t0 + i * q.step_h * HOUR;
      if (v != null && t >= from) series.push({ t: new Date(t), v });
    });
  }
  return { flow: reading(now.flow_cfs), stage: reading(now.stage_ft), temp: reading(now.water_temp_c), series, live };
}

/** The same shape as `liveGauge`, read from the validation-year record as if `at` were now. No stage is archived. */
export function archivedGauge(obs, at) {
  const t = Math.floor(at.getTime() / HOUR) * HOUR;
  // Winter records have multi-day ice gaps, so fall back to the latest reading in the past week, like a live gauge would.
  const latest = (series) => {
    for (let ms = t; ms >= t - 7 * DAY; ms -= HOUR) {
      const v = observedAt(series, ms);
      if (v != null) return { t: new Date(ms), v };
    }
    return null;
  };
  const series = [];
  for (let ms = t - 7 * DAY; ms <= t; ms += HOUR) {
    const v = observedAt(obs.flow, ms);
    if (v != null) series.push({ t: new Date(ms), v });
  }
  return { flow: latest(obs.flow), stage: null, temp: latest(obs.temp), series };
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

export function waterYear(d) {
  return d.getUTCMonth() >= 9 ? d.getUTCFullYear() + 1 : d.getUTCFullYear();
}
