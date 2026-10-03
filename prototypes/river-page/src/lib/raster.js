const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));

function rings(geometry) {
  if (geometry.type === 'Polygon') return [geometry.coordinates];
  if (geometry.type === 'MultiPolygon') return geometry.coordinates;
  return [];
}

export function outerRings(geometry) {
  return rings(geometry).map((p) => p[0]);
}

function inRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function inPolygon(geometry, [x, y]) {
  return rings(geometry).some((poly) => inRing(x, y, poly[0]) && !poly.slice(1).some((h) => inRing(x, y, h)));
}

export function basinMean(grid, values, geometry) {
  let s = 0;
  let n = 0;
  grid.pts.forEach((p, i) => {
    if (values[i] != null && inPolygon(geometry, p)) {
      s += values[i];
      n++;
    }
  });
  return n ? s / n : null;
}

export function sampleGrid(grid, values, lon, lat) {
  const gi = Math.min(grid.nx - 1.001, Math.max(0, (lon - grid.x0) / grid.step));
  const gj = Math.min(grid.ny - 1.001, Math.max(0, (lat - grid.y0) / grid.step));
  const i = Math.floor(gi);
  const j = Math.floor(gj);
  const fx = gi - i;
  const fy = gj - j;
  const corners = [
    [values[j * grid.nx + i], (1 - fx) * (1 - fy)],
    [values[j * grid.nx + i + 1], fx * (1 - fy)],
    [values[(j + 1) * grid.nx + i], (1 - fx) * fy],
    [values[(j + 1) * grid.nx + i + 1], fx * fy],
  ].filter(([v]) => v != null);
  const w = corners.reduce((s, [, k]) => s + k, 0);
  return w > 1e-6 ? corners.reduce((s, [v, k]) => s + v * k, 0) / w : null;
}

export function hexRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Linear ramp between two hex colors, t in [0, 1]. */
export function ramp([from, to], t) {
  const a = hexRgb(from);
  const b = hexRgb(to);
  const k = Math.max(0, Math.min(1, t));
  return a.map((x, i) => Math.round(x + (b[i] - x) * k));
}

/**
 * Smooth (bilinear) raster of a weather field over the grid, clipped to the basin, as a data URL plus the
 * lon/lat corners MapLibre's image source needs. `paint(v)` returns [r, g, b, alpha in 0..1].
 */
export function renderField(grid, values, geometry, paint, width = 560) {
  const x1 = grid.x0 + (grid.nx - 1) * grid.step;
  const y1 = grid.y0 + (grid.ny - 1) * grid.step;
  const m0 = mercY(grid.y0);
  const m1 = mercY(y1);
  const height = Math.round((width * (m1 - m0)) / (((x1 - grid.x0) * Math.PI) / 180));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(width, height);
  const data = img.data;

  // Hot loop: no allocations. Grid indices and weights are precomputed per column and per row, values are a
  // NaN-for-missing typed array, and colors come from a 256-step lookup table over the field's range.
  const nx = grid.nx;
  const vals = Float64Array.from(values, (v) => (v == null ? NaN : v));
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of vals) {
    if (v !== v) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  const span = hi - lo || 1;
  const lut = new Uint8ClampedArray(256 * 4);
  for (let k = 0; k < 256; k++) {
    const [r, g, b, a] = paint(lo + (k / 255) * span);
    lut.set([r, g, b, Math.round(255 * Math.max(0, Math.min(1, a)))], k * 4);
  }
  const colI = new Int32Array(width);
  const colF = new Float64Array(width);
  for (let px = 0; px < width; px++) {
    const g = Math.min(nx - 1.001, Math.max(0, (((px + 0.5) / width) * (x1 - grid.x0)) / grid.step));
    colI[px] = Math.floor(g);
    colF[px] = g - colI[px];
  }
  for (let py = 0; py < height; py++) {
    const m = m1 - ((py + 0.5) / height) * (m1 - m0);
    const lat = (Math.atan(Math.sinh(m)) * 180) / Math.PI;
    const gj = Math.min(grid.ny - 1.001, Math.max(0, (lat - grid.y0) / grid.step));
    const j = Math.floor(gj);
    const fy = gj - j;
    const row0 = j * nx;
    const row1 = row0 + nx;
    for (let px = 0; px < width; px++) {
      const i = colI[px];
      const fx = colF[px];
      const a = vals[row0 + i];
      const b = vals[row0 + i + 1];
      const c0 = vals[row1 + i];
      const d = vals[row1 + i + 1];
      const wa = a === a ? (1 - fx) * (1 - fy) : 0;
      const wb = b === b ? fx * (1 - fy) : 0;
      const wc = c0 === c0 ? (1 - fx) * fy : 0;
      const wd = d === d ? fx * fy : 0;
      const w = wa + wb + wc + wd;
      if (w < 1e-6) continue;
      const s = (wa ? a * wa : 0) + (wb ? b * wb : 0) + (wc ? c0 * wc : 0) + (wd ? d * wd : 0);
      const c = Math.max(0, Math.min(255, Math.round(((s / w - lo) / span) * 255))) * 4;
      const o = (py * width + px) * 4;
      data[o] = lut[c];
      data[o + 1] = lut[c + 1];
      data[o + 2] = lut[c + 2];
      data[o + 3] = lut[c + 3];
    }
  }
  ctx.putImageData(img, 0, 0);
  ctx.globalCompositeOperation = 'destination-in';
  ctx.beginPath();
  for (const ring of outerRings(geometry)) {
    ring.forEach(([lon, lat], i) => {
      const px = ((lon - grid.x0) / (x1 - grid.x0)) * width;
      const py = ((m1 - mercY(lat)) / (m1 - m0)) * height;
      if (i) ctx.lineTo(px, py);
      else ctx.moveTo(px, py);
    });
    ctx.closePath();
  }
  ctx.fill();
  return {
    url: canvas.toDataURL(),
    coordinates: [
      [grid.x0, y1],
      [x1, y1],
      [x1, grid.y0],
      [grid.x0, grid.y0],
    ],
  };
}
