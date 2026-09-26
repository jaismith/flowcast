import * as d3 from 'd3';
import './style.css';

const DATA_BASE = `${import.meta.env.BASE_URL}data/`;
const cache = new Map();

export function loadJSON(name) {
  if (!cache.has(name)) cache.set(name, d3.json(`${DATA_BASE}${name}`));
  return cache.get(name);
}

export const dataUrl = (name) => `${DATA_BASE}${name}`;

export const HOUR = 3600e3;
export const DAY = 24 * HOUR;

/** Expand a {start, stepHours, length} series header into Date objects. */
export function timeAxis({ start, stepHours = 1, length }) {
  const t0 = Date.parse(start);
  return Array.from({ length }, (_, i) => new Date(t0 + i * stepHours * HOUR));
}

export function mountTopbar(title, sub) {
  const el = document.createElement('header');
  el.className = 'topbar';
  el.innerHTML = `<a class="back" href="../" title="Back to gallery">&#8592;</a><div><h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  document.body.appendChild(el);
}

export function mountSource(html) {
  const el = document.createElement('div');
  el.className = 'source';
  el.innerHTML = html;
  document.body.appendChild(el);
  return el;
}

export function tooltip() {
  const el = document.createElement('div');
  el.className = 'tooltip';
  document.body.appendChild(el);
  return {
    show(html, event) {
      el.innerHTML = html;
      el.style.opacity = 1;
      const pad = 14;
      const { innerWidth: w, innerHeight: h } = window;
      const r = el.getBoundingClientRect();
      let x = event.clientX + pad;
      let y = event.clientY + pad;
      if (x + r.width > w - 8) x = event.clientX - r.width - pad;
      if (y + r.height > h - 8) y = event.clientY - r.height - pad;
      el.style.left = `${x}px`;
      el.style.top = `${y}px`;
    },
    hide() { el.style.opacity = 0; },
  };
}

export const fmtCfs = (v) => (v == null ? '–' : `${d3.format(',.0f')(v)} cfs`);
export const fmtF = (v) => (v == null ? '–' : `${v.toFixed(1)} °F`);
export const fmtIn = (v) => (v == null ? '–' : `${v.toFixed(2)} in`);
export const fmtDate = d3.utcFormat('%b %-d, %Y');

const etFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', hour12: true });
const etDay = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
/** Eastern-time formatting (the gauge's local time), regardless of the viewer's zone. */
export const fmtET = (d) => etFmt.format(d);
export const fmtETDay = (d) => etDay.format(d);

/** Resize-aware canvas at device pixel ratio. Returns a getter for CSS size. */
export function hiDpiCanvas(canvas, onResize) {
  const size = { w: 0, h: 0, dpr: 1 };
  const resize = () => {
    const r = canvas.getBoundingClientRect();
    size.dpr = Math.min(window.devicePixelRatio || 1, 2);
    size.w = r.width;
    size.h = r.height;
    canvas.width = Math.round(r.width * size.dpr);
    canvas.height = Math.round(r.height * size.dpr);
    canvas.getContext('2d').setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
    onResize?.(size);
  };
  new ResizeObserver(resize).observe(canvas);
  resize();
  return size;
}

export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
