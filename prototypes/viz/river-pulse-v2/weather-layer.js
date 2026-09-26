import * as d3 from 'd3';
import { solarPosition, sunVectorENU, SNOW_DENSITY_RATIO } from '../shared/hourly-grid.js';

const OW = 240; // offscreen field resolution (px across the terrain bbox)
const RAIN_STOPS = [[0.1, '#1b9e77'], [0.5, '#66c2a5'], [1, '#a6d854'], [3, '#ffd92f'], [6, '#fc8d62'], [12, '#e31a1c'], [25, '#c51b8a']];
const rainLUT = (() => {
  const lut = [];
  const scale = d3.scaleLog(RAIN_STOPS.map((s) => s[0]), RAIN_STOPS.map((s) => s[1])).clamp(true);
  for (let k = 0; k < 64; k++) {
    const mm = Math.exp(Math.log(0.1) + (k / 63) * (Math.log(25) - Math.log(0.1)));
    const c = d3.rgb(scale(mm));
    lut.push([c.r, c.g, c.b, 0.28 + 0.5 * (k / 63)]);
  }
  return lut;
})();
export const RAIN_LEGEND = RAIN_STOPS;

/**
 * Weather layer for River Pulse v2: radar-style rain field, sunlit-slope glow
 * (hillshade from the true sun position × gridded shortwave), snow cover and melt.
 */
export function createWeatherLayer(canvas, terrain, basin) {
  const ctx = canvas.getContext('2d');
  const OH = Math.round((OW * terrain.height) / terrain.width);
  const N = OW * OH;
  const off = document.createElement('canvas');
  off.width = OW;
  off.height = OH;
  const octx = off.getContext('2d');
  const img = octx.createImageData(OW, OH);

  // Terrain normals (east, north, up) from the unexaggerated DEM.
  const normals = new Float32Array(N * 3);
  const lonlat = new Float32Array(N * 2);
  const inBasin = new Uint8Array(N);
  {
    const [pm] = terrain.pixelMeters();
    const s = terrain.width / OW;
    const e = (x, y) => terrain.elev[Math.min(terrain.height - 1, Math.max(0, Math.round(y))) * terrain.width + Math.min(terrain.width - 1, Math.max(0, Math.round(x)))];
    const feature = basin.features[0];
    for (let j = 0; j < OH; j++) {
      for (let i = 0; i < OW; i++) {
        const k = j * OW + i;
        const x = (i + 0.5) * s;
        const y = (j + 0.5) * s;
        const dzde = (e(x + s, y) - e(x - s, y)) / (2 * s * pm);
        const dzdn = -(e(x, y + s) - e(x, y - s)) / (2 * s * pm);
        const len = Math.hypot(dzde, dzdn, 1);
        normals[k * 3] = -dzde / len;
        normals[k * 3 + 1] = -dzdn / len;
        normals[k * 3 + 2] = 1 / len;
        const [lon, lat] = terrain.uvToLonLat((i + 0.5) / OW, (j + 0.5) / OH);
        lonlat[k * 2] = lon;
        lonlat[k * 2 + 1] = lat;
        inBasin[k] = d3.geoContains(feature, [lon, lat]) ? 1 : 0;
      }
    }
  }
  const basinCenter = d3.geoCentroid(basin);

  let grid = null;
  let weights = null; // per-pixel bilinear cell weights for the current grid
  const cellTmp = {};
  const rainPx = new Float32Array(N);
  const snowfallPx = new Float32Array(N);
  const meltPx = new Float32Array(N);
  const cum = { rain: new Float32Array(N), snow: new Float32Array(N), melt: new Float32Array(N) };
  const totals = { rain: 0, snow: 0, melt: 0 };
  let rect = { x: 0, y: 0, w: 1, h: 1 };
  let sun = { azimuth: 180, elevation: 30 };
  let basinStats = { rain: 0, sw: 0, snow: 0, melt: 0 };

  function setGrid(g) {
    grid = g;
    if (!g) return;
    weights = new Float32Array(N * 8);
    for (let k = 0; k < N; k++) weights.set(g.weightsAt(lonlat[k * 2], lonlat[k * 2 + 1]), k * 8);
    const nc = g.nx * g.ny;
    for (const key of ['precip', 'snowDepth', 'sw', 'temp', 'snowPrev']) cellTmp[key] = new Float32Array(nc);
  }

  function layout(projection) {
    const [lon0, lat0, lon1, lat1] = terrain.meta.bbox;
    const [x0, y0] = projection([lon0, lat1]);
    const [x1, y1] = projection([lon1, lat0]);
    rect = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  // Interpolate every cell to fractional hour h once, then bilinear per pixel.
  function cellsAt(key, h, out) {
    const nc = grid.nx * grid.ny;
    const h0 = Math.max(0, Math.min(grid.hours - 1, Math.floor(h)));
    const h1 = Math.min(grid.hours - 1, h0 + 1);
    const f = Math.max(0, Math.min(1, h - h0));
    const a = grid.vars[key];
    for (let c = 0; c < nc; c++) out[c] = a[h0 * nc + c] * (1 - f) + a[h1 * nc + c] * f;
  }
  const bil = (arr, k) => {
    const o = k * 8;
    return arr[weights[o]] * weights[o + 4] + arr[weights[o + 1]] * weights[o + 5] + arr[weights[o + 2]] * weights[o + 6] + arr[weights[o + 3]] * weights[o + 7];
  };

  /** Recompute the field image for fractional grid hour h. */
  function compute(h, date, layers) {
    if (!grid) return;
    cellsAt('precip', h, cellTmp.precip);
    cellsAt('snowDepth', h, cellTmp.snowDepth);
    cellsAt('snowDepth', h - 1, cellTmp.snowPrev);
    cellsAt('sw', h, cellTmp.sw);
    cellsAt('temp', h, cellTmp.temp);
    sun = solarPosition(date, basinCenter[1], basinCenter[0]);
    const [se, sn, su] = sunVectorENU(sun);
    const sinEl = Math.max(0.12, su);
    const d = img.data;
    let tr = 0, ts = 0, tm = 0;
    let bRain = 0, bSw = 0, bSnow = 0, bMelt = 0, bn = 0;
    for (let k = 0; k < N; k++) {
      const P = Math.max(0, bil(cellTmp.precip, k));
      const S = Math.max(0, bil(cellTmp.snowDepth, k));
      const Sprev = Math.max(0, bil(cellTmp.snowPrev, k));
      const SW = Math.max(0, bil(cellTmp.sw, k));
      const T = bil(cellTmp.temp, k);
      const melt = Math.max(0, Sprev - S) * 1000 * SNOW_DENSITY_RATIO; // mm water per hour
      const inside = inBasin[k];
      const liquid = T > 0.5;
      rainPx[k] = inside && liquid ? P : 0;
      snowfallPx[k] = inside && !liquid ? P : 0;
      meltPx[k] = inside ? melt : 0;
      tr += rainPx[k]; ts += snowfallPx[k]; tm += meltPx[k];
      cum.rain[k] = tr; cum.snow[k] = ts; cum.melt[k] = tm;
      if (inside) { bRain += P; bSw += SW; bSnow += S; bMelt += melt; bn++; }

      // Composite (premultiplied) glow → snow → melt → rain.
      let r = 0, g = 0, b = 0, a = 0;
      const over = (cr, cg, cb, ca) => { r = cr * ca + r * (1 - ca); g = cg * ca + g * (1 - ca); b = cb * ca + b * (1 - ca); a = ca + a * (1 - ca); };
      const nx = normals[k * 3], ny = normals[k * 3 + 1], nz = normals[k * 3 + 2];
      const inc = Math.max(0, nx * se + ny * sn + nz * su);
      if (inside) {
        // Relief: shade from the real sun when it's up, otherwise a soft fixed NW light.
        const rel = su > 0 ? inc / Math.max(0.35, su) : Math.max(0, nx * -0.5 + ny * 0.5 + nz * 0.7);
        if (layers.sun) over(0, 0, 8, Math.min(0.55, Math.max(0, 1 - rel) * (su > 0 ? 0.5 : 0.25)));
        if (layers.sun && su > 0) {
          // Terrain-corrected shortwave: diffuse share + direct beam scaled by slope incidence.
          const I = (SW / 850) * (0.2 + (0.8 * inc) / sinEl);
          const aGlow = Math.min(0.55, Math.max(0, I - 0.35) * 0.55);
          if (aGlow > 0.01) over(255, 170 + Math.min(60, I * 30), 60, aGlow);
        }
        if (layers.snow && S > 0.005) {
          const lit = su > 0 ? 0.6 + 0.4 * Math.min(1, inc / Math.max(0.35, su)) : 0.5;
          over(225 * lit + 30, 232 * lit + 23, 255, Math.min(1, S / 0.3) * 0.5);
          if (melt > 0.1) over(215, 170, 255, Math.min(0.3, melt / 5));
        }
      }
      if (layers.rain && P > 0.1) {
        const li = Math.max(0, Math.min(63, Math.round(((Math.log(P) - Math.log(0.1)) / (Math.log(25) - Math.log(0.1))) * 63)));
        const c = rainLUT[li];
        if (liquid) over(c[0], c[1], c[2], c[3] * (inside ? 0.9 : 0));
        else over(230, 235, 255, c[3] * 0.7 * (inside ? 1 : 0));
      }
      const o = k * 4;
      d[o] = a > 0 ? r / a : 0; d[o + 1] = a > 0 ? g / a : 0; d[o + 2] = a > 0 ? b / a : 0; d[o + 3] = a * 255;
    }
    totals.rain = tr; totals.snow = ts; totals.melt = tm;
    basinStats = { rain: bRain / bn, sw: bSw / bn, snow: bSnow / bn, melt: bMelt / bn };
    octx.putImageData(img, 0, 0);
  }

  function draw() {
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(off, rect.x, rect.y, rect.w, rect.h);
  }

  /** Random screen point weighted by the given field ('rain' | 'snow' | 'melt'). */
  function sample(kind) {
    const c = cum[kind];
    const total = totals[kind];
    if (total <= 0) return null;
    const r = Math.random() * total;
    let lo = 0, hi = N - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (c[mid] < r) lo = mid + 1; else hi = mid; }
    const i = lo % OW, j = Math.floor(lo / OW);
    return [rect.x + ((i + Math.random()) / OW) * rect.w, rect.y + ((j + Math.random()) / OH) * rect.h];
  }

  return {
    setGrid, layout, compute, draw, sample,
    get totals() { return totals; },
    get sun() { return sun; },
    get stats() { return basinStats; },
    get pixelsInBasin() { return inBasin.reduce((s, v) => s + v, 0); },
  };
}
