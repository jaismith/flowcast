#!/usr/bin/env node
// Fetches public data for the viz prototypes and caches it as small static files
// in public/data/. Re-run with `npm run fetch-data` to refresh.
//
// Sources: USGS NWIS (waterservices), USGS NLDI, USGS NHDPlus V2 GeoServer,
// Open-Meteo (ERA5 archive, GFS ensemble, GloFAS flood), AWS Terrain Tiles,
// and the read-only flowcast /forecast endpoint. /report is never called.

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PNG } from 'pngjs';
import * as d3 from 'd3';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const OUT = path.join(ROOT, 'public', 'data');

const SITE = '01427510';
const SITE_COMID = 2617456;
const HUCS = '02040101,02040102';
const NLDI = 'https://api.water.usgs.gov/nldi/linked-data';
const WFS = 'https://api.water.usgs.gov/geoserver/wmadata/ows';
const NWIS = 'https://waterservices.usgs.gov/nwis';
const RAINDROP_START = [-74.5935, 42.4105]; // hillslope below Mt. Utsayantha, West Branch headwaters

const round = (v, dp = 4) => (v == null || Number.isNaN(v) ? null : Math.round(v * 10 ** dp) / 10 ** dp);
const roundCoords = (c, dp = 4) => (typeof c[0] === 'number' ? [round(c[0], dp), round(c[1], dp)] : c.map((x) => roundCoords(x, dp)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const HOUR = 3600e3;
const DAY = 24 * HOUR;
const isoDate = (d) => d.toISOString().slice(0, 10);

async function get(url, { json = true, tries = 4 } = {}) {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(120e3) });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return json ? await res.json() : Buffer.from(await res.arrayBuffer());
    } catch (err) {
      console.warn(`  retry ${i + 1}/${tries} ${url.slice(0, 120)}: ${err.message}`);
      if (i === tries - 1) throw err;
      await sleep(2000 * 2 ** i);
    }
  }
}

async function write(name, data) {
  const file = path.join(OUT, name);
  const body = Buffer.isBuffer(data) ? data : JSON.stringify(data);
  await fs.writeFile(file, body);
  console.log(`  wrote ${name} (${(body.length / 1024).toFixed(0)} KB)`);
}

// ---------- USGS NWIS ----------

function parseIV(json) {
  const bySite = new Map();
  for (const ts of json.value.timeSeries) {
    const si = ts.sourceInfo;
    const id = si.siteCode[0].value;
    const param = ts.variable.variableCode[0].value;
    const noData = ts.variable.noDataValue;
    const site = bySite.get(id) ?? {
      id,
      name: si.siteName,
      lat: si.geoLocation.geogLocation.latitude,
      lon: si.geoLocation.geogLocation.longitude,
      series: {},
    };
    // Some gauges report several sensors for one parameter; keep the longest.
    const values = ts.values
      .map((v) => v.value.map((p) => [Date.parse(p.dateTime), +p.value]).filter(([, x]) => x !== noData && x > -9999))
      .sort((a, b) => b.length - a.length)[0] ?? [];
    if (!site.series[param] || site.series[param].length < values.length) site.series[param] = values;
    bySite.set(id, site);
  }
  return [...bySite.values()];
}

/** Hourly means on a fixed grid [t0, t1) — null where no data. */
function hourly(points, t0, t1, step = HOUR) {
  const n = Math.round((t1 - t0) / step);
  const sum = new Float64Array(n);
  const cnt = new Uint16Array(n);
  for (const [t, v] of points) {
    const i = Math.floor((t - t0) / step);
    if (i >= 0 && i < n) { sum[i] += v; cnt[i]++; }
  }
  return Array.from(sum, (s, i) => (cnt[i] ? s / cnt[i] : null));
}

const cToF = (c) => (c == null ? null : c * 9 / 5 + 32);

async function siteInfo(ids) {
  const txt = (await get(`${NWIS}/site/?format=rdb&siteOutput=expanded&sites=${ids.join(',')}`, { json: false })).toString();
  const lines = txt.split('\n').filter((l) => l && !l.startsWith('#'));
  const header = lines[0].split('\t');
  const out = {};
  for (const l of lines.slice(2)) {
    const row = Object.fromEntries(l.split('\t').map((v, i) => [header[i], v]));
    out[row.site_no] = { drainageSqMi: row.drain_area_va ? +row.drain_area_va : null, altitudeFt: row.alt_va ? +row.alt_va : null };
  }
  return out;
}

// ---------- main ----------

async function main() {
  await fs.mkdir(OUT, { recursive: true });
  const now = Date.now();
  const tEnd = Math.floor(now / HOUR) * HOUR;

  console.log('Basin boundary (NLDI)');
  const basin = await get(`${NLDI}/nwissite/USGS-${SITE}/basin?simplified=true`);
  basin.features[0].geometry.coordinates = roundCoords(basin.features[0].geometry.coordinates);
  basin.features[0].properties = { site: SITE, name: 'Delaware River at Callicoon, NY' };
  await write('basin.json', basin);
  const basinFeature = basin.features[0];
  const [[bx0, by0], [bx1, by1]] = d3.geoBounds(basinFeature);
  const bbox = [bx0 - 0.02, by0 - 0.02, bx1 + 0.02, by1 + 0.02];

  console.log('Upstream network (NLDI UT comids + NHDPlus attributes)');
  const ut = await get(`${NLDI}/comid/${SITE_COMID}/navigation/UT/flowlines?distance=1000`);
  const upstream = new Set(ut.features.map((f) => +f.properties.nhdplus_comid));
  console.log(`  ${upstream.size} upstream flowlines`);
  const props = 'comid,gnis_name,streamorde,totdasqkm,hydroseq,dnhydroseq,qe_ma,ve_ma,slope,lengthkm,pathlength,maxelevsmo,minelevsmo,ftype,the_geom';
  const cql = encodeURIComponent(`streamorde>=2 AND BBOX(the_geom,${bbox.join(',')})`);
  const wfs = await get(`${WFS}?service=WFS&version=1.0.0&request=GetFeature&typeName=wmadata:nhdflowline_network&outputFormat=application/json&propertyName=${props}&CQL_FILTER=${cql}`);
  const rivers = {
    type: 'FeatureCollection',
    features: wfs.features
      .filter((f) => upstream.has(f.properties.comid))
      .map((f) => {
        const p = f.properties;
        const lines = f.geometry.type === 'MultiLineString' ? f.geometry.coordinates : [f.geometry.coordinates];
        return {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: roundCoords(lines.flat()) },
          properties: {
            comid: p.comid,
            name: p.gnis_name?.trim() || null,
            order: p.streamorde,
            areaSqKm: round(p.totdasqkm, 1),
            hydroseq: p.hydroseq,
            dnhydroseq: p.dnhydroseq,
            meanFlowCfs: round(p.qe_ma, 1),
            velocityFps: round(p.ve_ma, 2),
            slope: round(p.slope, 5),
            lengthKm: round(p.lengthkm, 3),
            pathKm: round(p.pathlength, 2),
            ftype: p.ftype,
          },
        };
      }),
  };
  console.log(`  ${rivers.features.length} order>=2 flowlines in basin`);
  await write('rivers.json', rivers);

  console.log('Reservoirs and lakes (NHDPlus waterbodies)');
  const wbCql = encodeURIComponent(`areasqkm>0.5 AND BBOX(the_geom,${bbox.join(',')})`);
  const wb = await get(`${WFS}?service=WFS&version=1.0.0&request=GetFeature&typeName=wmadata:nhdwaterbody&outputFormat=application/json&propertyName=comid,gnis_name,areasqkm,ftype,the_geom&CQL_FILTER=${wbCql}`);
  const waterbodies = {
    type: 'FeatureCollection',
    features: wb.features
      .filter((f) => d3.geoContains(basinFeature, d3.geoCentroid(f)))
      .map((f) => ({ type: 'Feature', geometry: { ...f.geometry, coordinates: roundCoords(f.geometry.coordinates) }, properties: { name: f.properties.gnis_name?.trim() || null, areaSqKm: round(f.properties.areasqkm, 2), ftype: f.properties.ftype } })),
  };
  await write('waterbodies.json', waterbodies);

  console.log('Upstream gauges (NLDI) + active IV discharge (NWIS)');
  const utSites = await get(`${NLDI}/nwissite/USGS-${SITE}/navigation/UT/nwissite?distance=1000`);
  const upstreamSites = new Set(utSites.features.map((f) => f.properties.identifier.replace('USGS-', '')));
  upstreamSites.add(SITE);

  // Callicoon: last 365 days hourly (drives storm-window selection, storm explorer)
  const yearStart = tEnd - 365 * DAY;
  const ivYear = parseIV(await get(`${NWIS}/iv/?format=json&sites=${SITE}&parameterCd=00060,00010&startDT=${new Date(yearStart).toISOString()}&endDT=${new Date(tEnd).toISOString()}`));
  const cal = ivYear[0];
  const calQ = hourly(cal.series['00060'] ?? [], yearStart, tEnd);
  const calWT = hourly(cal.series['00010'] ?? [], yearStart, tEnd).map((c) => round(cToF(c), 2));

  // Biggest event of the past year → storm window for the network map
  let peakI = 0;
  calQ.forEach((v, i) => { if (v != null && v > (calQ[peakI] ?? -1)) peakI = i; });
  const peakT = yearStart + peakI * HOUR;
  const windows = {
    storm: { label: `Biggest flow of the past year (${isoDate(new Date(peakT))})`, t0: peakT - 12 * DAY, t1: Math.min(peakT + 18 * DAY, tEnd) },
    recent: { label: 'Last 30 days', t0: tEnd - 30 * DAY, t1: tEnd },
  };

  const gaugeMeta = {};
  for (const [key, w] of Object.entries(windows)) {
    const sites = parseIV(await get(`${NWIS}/iv/?format=json&huc=${HUCS}&parameterCd=00060,00010&siteStatus=all&startDT=${new Date(w.t0).toISOString()}&endDT=${new Date(w.t1).toISOString()}`))
      .filter((s) => upstreamSites.has(s.id) && s.series['00060']?.length > 100);
    const meta = await siteInfo(sites.map((s) => s.id));
    Object.assign(gaugeMeta, meta);
    const out = {
      label: w.label,
      start: new Date(w.t0).toISOString(),
      stepHours: 1,
      length: Math.round((w.t1 - w.t0) / HOUR),
      sites: sites.map((s) => ({
        id: s.id,
        name: s.name,
        lat: s.lat,
        lon: s.lon,
        drainageSqMi: meta[s.id]?.drainageSqMi ?? null,
        q: hourly(s.series['00060'], w.t0, w.t1).map((v) => round(v, 1)),
        wt: s.series['00010'] ? hourly(s.series['00010'], w.t0, w.t1).map((c) => round(cToF(c), 1)) : null,
      })),
    };
    console.log(`  ${key}: ${out.sites.length} gauges, ${out.length} h`);
    await write(`gauges-${key}.json`, out);
  }

  console.log('Callicoon daily history (NWIS DV)');
  const dv = parseIV(await get(`${NWIS}/dv/?format=json&sites=${SITE}&parameterCd=00060,00010&statCd=00003&startDT=1975-10-01`))[0];
  const dvStart = Math.min(...Object.values(dv.series).map((s) => s[0][0]));
  const dayKey = (t) => isoDate(new Date(t + 12 * HOUR));
  const dvQ = new Map(dv.series['00060'].map(([t, v]) => [dayKey(t), v]));
  const dvW = new Map((dv.series['00010'] ?? []).map(([t, v]) => [dayKey(t), v]));
  const days = d3.utcDay.range(new Date(dayKey(dvStart)), new Date(tEnd));
  // Fill the last few days (DV lags) from the hourly IV record.
  const ivDaily = d3.rollup(calQ.map((v, i) => [dayKey(yearStart + i * HOUR - 5 * HOUR), v]).filter(([, v]) => v != null), (a) => d3.mean(a, (d) => d[1]), (d) => d[0]);
  const ivDailyW = d3.rollup(calWT.map((v, i) => [dayKey(yearStart + i * HOUR - 5 * HOUR), v]).filter(([, v]) => v != null), (a) => d3.mean(a, (d) => d[1]), (d) => d[0]);
  await write('callicoon-daily.json', {
    site: SITE,
    start: isoDate(days[0]),
    q: days.map((d) => { const k = isoDate(d); return round(dvQ.get(k) ?? ivDaily.get(k) ?? null, 0); }),
    wt: days.map((d) => { const k = isoDate(d); const c = dvW.get(k); return c != null ? round(cToF(c), 1) : round(ivDailyW.get(k) ?? null, 1); }),
    units: { q: 'cfs', wt: '°F' },
    note: 'NWIS daily means; the most recent days (not yet published as DV) are filled from provisional instantaneous values.',
  });

  console.log('Weather grid over the basin (Open-Meteo ERA5 archive)');
  const grid = [];
  for (let lat = Math.ceil(by0 * 10) / 10; lat <= by1; lat += 0.1) {
    for (let lon = Math.ceil(bx0 * 10) / 10; lon <= bx1; lon += 0.125) {
      if (d3.geoContains(basinFeature, [lon, lat])) grid.push([round(lon, 3), round(lat, 3)]);
    }
  }
  console.log(`  ${grid.length} grid points inside basin`);
  const archiveEnd = isoDate(new Date(tEnd - 6 * DAY));
  const archiveStart = isoDate(new Date(yearStart));
  const hourlyVars = ['precipitation', 'snowfall', 'snow_depth', 'temperature_2m', 'shortwave_radiation'];
  const gridHourly = [];
  for (let i = 0; i < grid.length; i += 10) {
    const chunk = grid.slice(i, i + 10);
    const url = `https://archive-api.open-meteo.com/v1/archive?latitude=${chunk.map((p) => p[1]).join(',')}&longitude=${chunk.map((p) => p[0]).join(',')}&start_date=${archiveStart}&end_date=${archiveEnd}&hourly=${hourlyVars.join(',')}&timezone=GMT&precipitation_unit=inch&temperature_unit=fahrenheit`;
    const res = await get(url);
    gridHourly.push(...(Array.isArray(res) ? res : [res]));
    await sleep(1500);
  }
  const wxTimes = gridHourly[0].hourly.time.map((t) => Date.parse(`${t}Z`));
  const nWx = wxTimes.length;
  const basinMean = Object.fromEntries(hourlyVars.map((v) => [v, Array.from({ length: nWx }, (_, i) => round(d3.mean(gridHourly, (g) => g.hourly[v][i]), 3))]));
  await write('weather-basin-hourly.json', {
    start: new Date(wxTimes[0]).toISOString(),
    stepHours: 1,
    length: nWx,
    points: grid.length,
    source: 'Open-Meteo historical weather API (ERA5 / ERA5-Land reanalysis), mean of grid points inside the basin',
    units: { precipitation: 'in', snowfall: 'cm', snow_depth: 'm', temperature_2m: '°F', shortwave_radiation: 'W/m²' },
    ...basinMean,
  });
  // Daily per-point grid for the 3D terrain
  const nDays = Math.floor(nWx / 24);
  await write('weather-grid-daily.json', {
    start: archiveStart,
    days: nDays,
    points: grid,
    units: { precip: 'in/day', snowfall: 'cm/day', snowDepth: 'm', temp: '°F', radiation: 'MJ/m²/day' },
    precip: gridHourly.map((g) => d3.range(nDays).map((d) => round(d3.sum(g.hourly.precipitation.slice(d * 24, d * 24 + 24)), 3))),
    snowfall: gridHourly.map((g) => d3.range(nDays).map((d) => round(d3.sum(g.hourly.snowfall.slice(d * 24, d * 24 + 24)), 2))),
    snowDepth: gridHourly.map((g) => d3.range(nDays).map((d) => round(d3.mean(g.hourly.snow_depth.slice(d * 24, d * 24 + 24)), 3))),
    temp: gridHourly.map((g) => d3.range(nDays).map((d) => round(d3.mean(g.hourly.temperature_2m.slice(d * 24, d * 24 + 24)), 1))),
    radiation: gridHourly.map((g) => d3.range(nDays).map((d) => round(d3.sum(g.hourly.shortwave_radiation.slice(d * 24, d * 24 + 24)) * 0.0036, 2))),
  });
  // Hourly Callicoon record aligned to its own start
  await write('callicoon-hourly.json', {
    site: SITE,
    start: new Date(yearStart).toISOString(),
    stepHours: 1,
    length: calQ.length,
    q: calQ.map((v) => round(v, 0)),
    wt: calWT,
    units: { q: 'cfs', wt: '°F' },
  });

  console.log('Terrain (AWS Terrain Tiles, terrarium encoding)');
  const Z = 10;
  const lon2x = (lon) => ((lon + 180) / 360) * 2 ** Z;
  const lat2y = (lat) => ((1 - Math.log(Math.tan((lat * Math.PI) / 180) + 1 / Math.cos((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** Z;
  const tb = [bx0 - 0.05, by0 - 0.05, bx1 + 0.05, by1 + 0.05];
  const [x0, x1] = [Math.floor(lon2x(tb[0])), Math.floor(lon2x(tb[2]))];
  const [y0, y1] = [Math.floor(lat2y(tb[3])), Math.floor(lat2y(tb[1]))];
  const mosaicW = (x1 - x0 + 1) * 256;
  const mosaicH = (y1 - y0 + 1) * 256;
  const elev = new Float32Array(mosaicW * mosaicH);
  for (let tx = x0; tx <= x1; tx++) {
    for (let ty = y0; ty <= y1; ty++) {
      const png = PNG.sync.read(await get(`https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${Z}/${tx}/${ty}.png`, { json: false }));
      for (let py = 0; py < 256; py++) {
        for (let px = 0; px < 256; px++) {
          const k = (py * 256 + px) * 4;
          const e = png.data[k] * 256 + png.data[k + 1] + png.data[k + 2] / 256 - 32768;
          elev[((ty - y0) * 256 + py) * mosaicW + (tx - x0) * 256 + px] = e;
        }
      }
    }
  }
  // Crop to bbox in mercator pixel space, then resample to a modest grid.
  const cx0 = (lon2x(tb[0]) - x0) * 256, cx1 = (lon2x(tb[2]) - x0) * 256;
  const cy0 = (lat2y(tb[3]) - y0) * 256, cy1 = (lat2y(tb[1]) - y0) * 256;
  const W = 640;
  const H = Math.round((W * (cy1 - cy0)) / (cx1 - cx0));
  const out = new PNG({ width: W, height: H });
  let eMin = Infinity, eMax = -Infinity;
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const sx = Math.min(mosaicW - 1, Math.round(cx0 + ((i + 0.5) / W) * (cx1 - cx0)));
      const sy = Math.min(mosaicH - 1, Math.round(cy0 + ((j + 0.5) / H) * (cy1 - cy0)));
      const e = elev[sy * mosaicW + sx];
      eMin = Math.min(eMin, e); eMax = Math.max(eMax, e);
      const v = e + 32768;
      const k = (j * W + i) * 4;
      out.data[k] = Math.floor(v / 256);
      out.data[k + 1] = Math.floor(v) % 256;
      out.data[k + 2] = Math.floor((v % 1) * 256);
      out.data[k + 3] = 255;
    }
  }
  await write('terrain.png', PNG.sync.write(out));
  await write('terrain.json', { bbox: tb, width: W, height: H, zoom: Z, projection: 'web-mercator (rows linear in mercator y)', encoding: 'terrarium: elev = R*256 + G + B/256 - 32768 (m)', minElevM: round(eMin, 1), maxElevM: round(eMax, 1) });

  console.log('Forecasts: flowcast live /forecast (read-only), GloFAS ensemble, GFS ensemble precip');
  const flowcast = await get(`https://api.flowcast.jaismith.dev/forecast?usgs_site=${SITE}`);
  await write('forecast-flowcast.json', { fetchedAt: new Date(now).toISOString(), source: 'GET https://api.flowcast.jaismith.dev/forecast?usgs_site=01427510', ...flowcast });
  // The nearest ~5 km GloFAS cell is often a tributary, so pick the neighbouring
  // cell whose recent discharge best matches the observed gauge record.
  const obsMeanCfs = d3.mean(calQ.slice(-60 * 24).filter((v) => v != null));
  const offsets = d3.range(-3, 4).map((k) => k * 0.05);
  const cands = offsets.flatMap((dy) => offsets.map((dx) => [round(cal.lat + dy, 3), round(cal.lon + dx, 3)]));
  const probe = await get(`https://flood-api.open-meteo.com/v1/flood?latitude=${cands.map((c) => c[0]).join(',')}&longitude=${cands.map((c) => c[1]).join(',')}&daily=river_discharge&past_days=60&forecast_days=1`);
  const best = probe
    .map((p, i) => ({ c: cands[i], err: Math.abs(Math.log((d3.mean(p.daily.river_discharge.filter((v) => v != null)) * 35.3147) / obsMeanCfs)), dist: Math.hypot(cands[i][0] - cal.lat, cands[i][1] - cal.lon) }))
    .sort((a, b) => a.err + a.dist - (b.err + b.dist))[0];
  console.log(`  GloFAS cell ${best.c} (log-mean error ${best.err.toFixed(2)}, observed mean ${obsMeanCfs.toFixed(0)} cfs)`);
  const glofas = await get(`https://flood-api.open-meteo.com/v1/flood?latitude=${best.c[0]}&longitude=${best.c[1]}&daily=river_discharge&ensemble=true&forecast_days=30&past_days=60`);
  const members = Object.keys(glofas.daily).filter((k) => k.startsWith('river_discharge')).map((k) => glofas.daily[k].map((v) => round(v * 35.3147, 0)));
  await write('forecast-glofas.json', {
    fetchedAt: new Date(now).toISOString(),
    source: 'Open-Meteo Flood API (Copernicus GloFAS v4, ~5 km), nearest river cell to the gauge',
    cell: { lat: glofas.latitude, lon: glofas.longitude },
    units: 'cfs (converted from m³/s)',
    time: glofas.daily.time,
    members,
  });
  const [clon, clat] = d3.geoCentroid(basinFeature);
  const ens = await get(`https://ensemble-api.open-meteo.com/v1/ensemble?latitude=${clat}&longitude=${clon}&hourly=precipitation,temperature_2m&models=gfs_seamless&forecast_days=8&precipitation_unit=inch&temperature_unit=fahrenheit&timezone=GMT`);
  const ensKeys = (v) => Object.keys(ens.hourly).filter((k) => k === v || k.startsWith(`${v}_member`));
  await write('forecast-gfs-ensemble.json', {
    fetchedAt: new Date(now).toISOString(),
    source: 'Open-Meteo Ensemble API (NOAA GEFS, 31 members), basin centroid',
    point: { lat: round(clat, 3), lon: round(clon, 3) },
    time: ens.hourly.time,
    precipitation: ensKeys('precipitation').map((k) => ens.hourly[k]),
    temperature: ensKeys('temperature_2m').map((k) => ens.hourly[k]),
  });

  console.log("Raindrop's journey path (NLDI)");
  const pos = await get(`${NLDI}/comid/position?coords=${encodeURIComponent(`POINT(${RAINDROP_START[0]} ${RAINDROP_START[1]})`)}`);
  const startComid = +pos.features[0].properties.comid;
  const dm = await get(`${NLDI}/comid/${startComid}/navigation/DM/flowlines?distance=400`);
  const dmComids = dm.features.map((f) => f.properties.nhdplus_comid);
  const byComid = new Map();
  for (let i = 0; i < dmComids.length; i += 40) {
    const cqlIn = encodeURIComponent(`comid IN (${dmComids.slice(i, i + 40).join(',')})`);
    const attrs = await get(`${WFS}?service=WFS&version=1.0.0&request=GetFeature&typeName=wmadata:nhdflowline_network&outputFormat=application/json&propertyName=comid,gnis_name,hydroseq,pathlength,lengthkm,qe_ma,ve_ma,totdasqkm,maxelevsmo,minelevsmo&CQL_FILTER=${cqlIn}`);
    for (const f of attrs.features) byComid.set(f.properties.comid, f.properties);
  }
  const segs = dm.features
    .map((f) => ({ comid: +f.properties.nhdplus_comid, coords: f.geometry.type === 'MultiLineString' ? f.geometry.coordinates.flat() : f.geometry.coordinates, p: byComid.get(+f.properties.nhdplus_comid) }))
    .filter((s) => s.p)
    .sort((a, b) => b.p.hydroseq - a.p.hydroseq);
  const siteSeg = segs.findIndex((s) => s.comid === SITE_COMID);
  const pathSegs = segs.slice(0, siteSeg + 1);
  const coords = [];
  for (const s of pathSegs) for (const c of s.coords) {
    const last = coords[coords.length - 1];
    if (!last || last[0] !== c[0] || last[1] !== c[1]) coords.push(c);
  }
  await write('raindrop-path.json', {
    start: RAINDROP_START,
    startComid,
    path: { type: 'Feature', geometry: { type: 'LineString', coordinates: roundCoords(coords, 5) }, properties: {} },
    segments: pathSegs.map((s) => ({
      comid: s.comid,
      name: s.p.gnis_name?.trim() || null,
      lengthKm: round(s.p.lengthkm, 3),
      meanFlowCfs: round(s.p.qe_ma, 1),
      velocityFps: round(s.p.ve_ma, 2),
      areaSqKm: round(s.p.totdasqkm, 1),
      elevM: round((s.p.maxelevsmo ?? 0) / 100, 1),
    })),
  });
  console.log(`  path: ${pathSegs.length} flowlines, ${round(d3.sum(pathSegs, (s) => s.p.lengthkm), 1)} km`);

  await write('manifest.json', { fetchedAt: new Date(now).toISOString(), site: SITE, gaugeMeta });
  console.log('Done.');
}

main().catch((err) => { console.error(err); process.exit(1); });
