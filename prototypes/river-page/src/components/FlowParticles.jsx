import { useEffect, useRef } from 'react';
import { C } from '../lib/palette.js';

const MIN_ORDER = 3;
// Screen-space look, the same at every zoom: spacing along a river, speed, tail, and the fade at path ends.
const SPACING_PX = { 3: 50, 4: 38, 5: 30, 6: 26, 7: 24 };
const SPEED_PX = { 3: 9, 4: 12, 5: 15, 6: 17, 7: 19 };
const TAIL_PX = 3.5;
const EDGE_FADE_PX = 10;
// Seeded densely enough for this zoom; zoomed further out, an evenly spread subset is shown.
const DENSE_ZOOM = 12;

/**
 * Specks gliding downstream along the larger streams, drawn on a canvas over the map so the motion is smooth at
 * any zoom (a stepped dash pattern on one-pixel lines reads as flashing). Spacing, speed and tail are in screen
 * pixels, and specks fade out over the markers in `avoid` ([lng, lat, radius px]). NHDPlus flowlines run
 * upstream to downstream. Stopped while off screen and for reduced motion.
 */
export default function FlowParticles({ map, rivers, avoid }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!map) return;
    const canvas = ref.current;
    const ctx = canvas.getContext('2d');
    const paths = buildPaths(rivers);
    const lat0 = (paths[0]?.pts[0][1] ?? 42) * (Math.PI / 180);
    // Metres per CSS pixel at the basin's latitude (MapLibre uses 512 px tiles).
    const mppAt = (z) => (78271.517 * Math.cos(lat0)) / 2 ** z;
    const parts = seed(paths, mppAt(DENSE_ZOOM));
    // The streak color at zero alpha, for the transparent end of each streak's tail-to-head fade.
    const clear = `${C.card.slice(0, 7)}00`;

    let w = 0;
    let h = 0;
    const size = () => {
      const dpr = window.devicePixelRatio || 1;
      ({ width: w, height: h } = canvas.getBoundingClientRect());
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const draw = (dt) => {
      const mpp = mppAt(map.getZoom());
      const shown = Math.min(1, mppAt(DENSE_ZOOM) / mpp);
      const marks = avoid.map(([lng, lat, r]) => ({ ...map.project([lng, lat]), r }));
      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      for (const p of parts) {
        const path = paths[p.path];
        p.d = (p.d + SPEED_PX[path.order] * mpp * dt) % path.len;
        // Fading in over the top fifth of the shown ranks keeps zooming from popping specks in and out.
        const zoomFade = Math.min(1, (shown - p.rank) / (0.2 * shown));
        if (zoomFade <= 0) continue;
        const head = map.project(pointAt(path, p.d, segmentAt(path, p.d)));
        if (head.x < -20 || head.y < -20 || head.x > w + 20 || head.y > h + 20) continue;
        let alpha = (0.35 + 0.1 * (path.order - MIN_ORDER)) * zoomFade * Math.min(1, Math.min(p.d, path.len - p.d) / (EDGE_FADE_PX * mpp));
        for (const m of marks) alpha *= Math.min(1, Math.max(0, (Math.hypot(head.x - m.x, head.y - m.y) - m.r) / 4));
        if (alpha <= 0.01) continue;
        ctx.globalAlpha = alpha;
        ctx.lineWidth = path.order >= 5 ? 1.8 : 1.3;
        const pts = stretch(path, p.d - TAIL_PX * mpp, p.d).map((q) => map.project(q));
        const tail = pts[0];
        const fade = ctx.createLinearGradient(tail.x, tail.y, head.x, head.y);
        fade.addColorStop(0, clear);
        fade.addColorStop(1, C.card);
        ctx.strokeStyle = fade;
        ctx.beginPath();
        pts.forEach(({ x, y }, i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }
    };

    let raf = 0;
    let last = 0;
    const frame = (t) => {
      raf = requestAnimationFrame(frame);
      draw(Math.min(0.05, (t - (last || t)) / 1000));
      last = t;
    };
    const start = () => {
      if (raf) return;
      last = 0;
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };

    let visible = false;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => {
      if (visible && !reduce.matches) start();
      else stop();
      if (reduce.matches) ctx.clearRect(0, 0, w, h);
    };
    const io = new IntersectionObserver(([e]) => {
      visible = e.isIntersecting;
      update();
    });
    // Redrawn without advancing on every camera change, so the specks stay on the rivers while panning.
    const follow = () => raf && draw(0);

    size();
    map.on('resize', size);
    map.on('move', follow);
    reduce.addEventListener('change', update);
    io.observe(canvas);
    return () => {
      stop();
      io.disconnect();
      reduce.removeEventListener('change', update);
      map.off('resize', size);
      map.off('move', follow);
    };
  }, [map, rivers, avoid]);
  return <canvas ref={ref} className="pointer-events-none absolute inset-0 size-full" aria-hidden />;
}

/**
 * Streams of order MIN_ORDER and up, with each segment joined to the next one downstream of the same order, so
 * specks run the length of a stream instead of looping on every short NHDPlus segment.
 */
function buildPaths(rivers) {
  const lines = rivers.features.filter((f) => f.geometry.type === 'LineString' && (f.properties.order ?? 0) >= MIN_ORDER);
  const key = ([x, y]) => `${x.toFixed(5)},${y.toFixed(5)}`;
  const byStart = new Map(lines.map((f) => [key(f.geometry.coordinates[0]), f]));
  const next = (f) => {
    const n = byStart.get(key(f.geometry.coordinates.at(-1)));
    return n?.properties.order === f.properties.order ? n : null;
  };
  const continued = new Set(lines.map(next).filter(Boolean));
  const used = new Set();
  const out = [];
  for (const head of lines) {
    if (continued.has(head)) continue;
    const pts = [];
    for (let f = head; f && !used.has(f); f = next(f)) {
      used.add(f);
      pts.push(...f.geometry.coordinates.slice(pts.length ? 1 : 0));
    }
    const cum = [0];
    for (let i = 1; i < pts.length; i++) {
      const kx = 111320 * Math.cos((pts[i][1] * Math.PI) / 180);
      cum.push(cum[i - 1] + Math.hypot((pts[i][0] - pts[i - 1][0]) * kx, (pts[i][1] - pts[i - 1][1]) * 110540));
    }
    if (cum.at(-1) > 300) out.push({ pts, cum, len: cum.at(-1), order: Math.min(7, head.properties.order) });
  }
  return out;
}

/** Evenly spaced specks at the dense zoom, ranked so any leading fraction of ranks is still evenly spread. */
function seed(paths, mpp) {
  const parts = [];
  paths.forEach((path, i) => {
    const spacing = SPACING_PX[path.order] * mpp;
    const n = Math.max(1, Math.round(path.len / spacing));
    const phase = Math.random() * spacing;
    const offset = Math.floor(Math.random() * 1024);
    for (let k = 0; k < n; k++) parts.push({ path: i, d: (phase + k * spacing) % path.len, rank: vanDerCorput(k + offset) });
  });
  return parts;
}

function vanDerCorput(k) {
  let r = 0;
  for (let b = 0.5; k; k >>= 1, b /= 2) if (k & 1) r += b;
  return r;
}

/** The part of a path between distances `d0` and `d1`, through every vertex in between, so a tail bends with the river. */
function stretch(path, d0, d1) {
  const a = segmentAt(path, Math.max(0, d0));
  const b = segmentAt(path, d1);
  return [pointAt(path, Math.max(0, d0), a), ...path.pts.slice(a + 1, b + 1), pointAt(path, d1, b)];
}

/** Index of the vertex that starts the segment containing distance `d`. */
function segmentAt({ cum }, d) {
  let lo = 0;
  let hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid;
    else hi = mid;
  }
  return lo;
}

function pointAt({ pts, cum }, d, lo) {
  const hi = lo + 1;
  const f = (d - cum[lo]) / (cum[hi] - cum[lo] || 1);
  return [pts[lo][0] + (pts[hi][0] - pts[lo][0]) * f, pts[lo][1] + (pts[hi][1] - pts[lo][1]) * f];
}
