import * as d3 from 'd3';
import { loadJSON, dataUrl, HOUR, clamp } from './common.js';

const MISSING = -32768;

/**
 * Hourly 0.1° weather grid written by scripts/fetch-data.mjs (ECMWF IFS 9 km via Open-Meteo).
 * Values between cell centers are bilinearly interpolated, and linearly between hours.
 */
export async function loadHourlyGrid(key) {
  const [meta, buf] = await Promise.all([
    loadJSON(`grid-hourly-${key}.json`),
    fetch(dataUrl(`grid-hourly-${key}.bin`)).then((r) => r.arrayBuffer()),
  ]);
  const raw = new Int16Array(buf);
  const { nx, ny, hours } = meta;
  const nc = nx * ny;
  const vars = {};
  meta.vars.forEach((v, vi) => {
    const arr = new Float32Array(hours * nc);
    for (let k = 0; k < arr.length; k++) {
      const x = raw[vi * hours * nc + k];
      arr[k] = x === MISSING ? NaN : x / v.scale;
    }
    vars[v.key] = arr;
  });
  const t0 = Date.parse(meta.start);
  const latTop = meta.lat0 + (ny - 1) * meta.dLat;
  const basinCells = meta.inBasin.map((b, c) => (b ? c : -1)).filter((c) => c >= 0);

  const grid = {
    meta, t0, hours, nx, ny, vars, basinCells,
    /** fractional hour index for a timestamp */
    hourAt: (ms) => (ms - t0) / HOUR,
    cellLonLat: (c) => [meta.lon0 + (c % nx) * meta.dLon, latTop - Math.floor(c / nx) * meta.dLat],
    /** Precompute the 4 cells and weights for a lon/lat (reuse for many samples). */
    weightsAt(lon, lat) {
      const fx = clamp((lon - meta.lon0) / meta.dLon, 0, nx - 1.0001);
      const fy = clamp((latTop - lat) / meta.dLat, 0, ny - 1.0001);
      const i = Math.floor(fx), j = Math.floor(fy);
      const ax = fx - i, ay = fy - j;
      const c = j * nx + i;
      return [c, c + 1, c + nx, c + nx + 1, (1 - ax) * (1 - ay), ax * (1 - ay), (1 - ax) * ay, ax * ay];
    },
    /** Value of a variable at fractional hour h using precomputed weights. */
    at(key, h, w) {
      const arr = vars[key];
      const h0 = clamp(Math.floor(h), 0, hours - 1);
      const h1 = Math.min(hours - 1, h0 + 1);
      const f = clamp(h - h0, 0, 1);
      const o0 = h0 * nc, o1 = h1 * nc;
      const v0 = arr[o0 + w[0]] * w[4] + arr[o0 + w[1]] * w[5] + arr[o0 + w[2]] * w[6] + arr[o0 + w[3]] * w[7];
      const v1 = arr[o1 + w[0]] * w[4] + arr[o1 + w[1]] * w[5] + arr[o1 + w[2]] * w[6] + arr[o1 + w[3]] * w[7];
      return v0 + (v1 - v0) * f;
    },
    cell(key, hInt, c) { return vars[key][clamp(hInt, 0, hours - 1) * nc + c]; },
    /** Basin-mean hourly series for a variable. */
    basinMean(key) {
      return d3.range(hours).map((h) => d3.mean(basinCells, (c) => vars[key][h * nc + c]));
    },
  };
  return grid;
}

/**
 * Approximate solar position (NOAA low-precision algorithm, ~0.5° accuracy).
 * Returns azimuth in degrees clockwise from north and elevation above the horizon.
 */
export function solarPosition(date, lat, lon) {
  const rad = Math.PI / 180;
  const n = date.getTime() / 864e5 + 2440587.5 - 2451545.0;
  const L = (280.46 + 0.9856474 * n) % 360;
  const g = ((357.528 + 0.9856003 * n) % 360) * rad;
  const lambda = (L + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad;
  const eps = (23.439 - 0.0000004 * n) * rad;
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda));
  const dec = Math.asin(Math.sin(eps) * Math.sin(lambda));
  const gmst = (((18.697374558 + 24.06570982441908 * n) % 24) + 24) % 24;
  const ha = (gmst * 15 + lon) * rad - ra;
  const phi = lat * rad;
  const elev = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(ha));
  const az = Math.atan2(Math.sin(ha), Math.cos(ha) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi)) / rad + 180;
  return { azimuth: ((az % 360) + 360) % 360, elevation: elev / rad };
}

/** Unit vector toward the sun in an east/north/up frame. */
export function sunVectorENU({ azimuth, elevation }) {
  const a = (azimuth * Math.PI) / 180;
  const e = (elevation * Math.PI) / 180;
  return [Math.sin(a) * Math.cos(e), Math.cos(a) * Math.cos(e), Math.sin(e)];
}

/** Decode terrain.png (terrarium) into elevation meters. */
export async function loadTerrain() {
  const meta = await loadJSON('terrain.json');
  const img = await new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = rej;
    im.src = dataUrl('terrain.png');
  });
  const cv = document.createElement('canvas');
  cv.width = meta.width;
  cv.height = meta.height;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, meta.width, meta.height).data;
  const elev = new Float32Array(meta.width * meta.height);
  for (let k = 0; k < elev.length; k++) elev[k] = px[k * 4] * 256 + px[k * 4 + 1] + px[k * 4 + 2] / 256 - 32768;
  const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
  const [lon0, lat0, lon1, lat1] = meta.bbox;
  const my0 = merc(lat1), my1 = merc(lat0);
  return {
    meta, elev, width: meta.width, height: meta.height,
    /** image fraction (u right, v down) → lon/lat */
    uvToLonLat: (u, v) => [lon0 + u * (lon1 - lon0), (Math.atan(Math.sinh(my0 + v * (my1 - my0))) * 180) / Math.PI],
    lonLatToUV: ([lon, lat]) => [(lon - lon0) / (lon1 - lon0), (merc(lat) - my0) / (my1 - my0)],
    /** ground meters per image pixel in x and y (at the bbox center latitude) */
    pixelMeters: () => {
      const latC = (lat0 + lat1) / 2;
      const mx = ((lon1 - lon0) * 111320 * Math.cos((latC * Math.PI) / 180)) / meta.width;
      return [mx, mx];
    },
  };
}

export const SNOW_DENSITY_RATIO = 0.3;
