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
  for (let py = 0; py < height; py++) {
    const m = m1 - ((py + 0.5) / height) * (m1 - m0);
    const lat = (Math.atan(Math.sinh(m)) * 180) / Math.PI;
    for (let px = 0; px < width; px++) {
      const lon = grid.x0 + ((px + 0.5) / width) * (x1 - grid.x0);
      const v = sampleGrid(grid, values, lon, lat);
      if (v == null) continue;
      const [r, g, b, a] = paint(v);
      const k = (py * width + px) * 4;
      img.data[k] = r;
      img.data[k + 1] = g;
      img.data[k + 2] = b;
      img.data[k + 3] = Math.round(255 * Math.max(0, Math.min(1, a)));
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
