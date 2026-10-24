import * as d3 from 'd3';

export const TZ = 'America/New_York';
export const FEATURED = [
  { id: '01427510', slug: 'callicoon', short: 'Callicoon' },
  { id: '01011000', slug: 'allagash', short: 'Allagash' },
  { id: '01654000', slug: 'accotink', short: 'Accotink Creek' },
];

const cache = new Map();
export function loadJSON(path) {
  if (!cache.has(path)) {
    cache.set(path, fetch(`./data/${path}`).then((r) => {
      if (!r.ok) throw new Error(`${path}: ${r.status}`);
      return r.json();
    }));
  }
  return cache.get(path);
}

export function siteHref(id) {
  return `./site.html?site=${id}`;
}

export function siteFromLocation() {
  const q = new URLSearchParams(location.search).get('site') || '01427510';
  const hit = FEATURED.find((s) => s.id === q || s.slug === q);
  return hit ? hit.id : '01427510';
}

// ---------- units ----------
const UNIT_KEY = 'flowcast-temp-unit';
let tempUnit = localStorage.getItem(UNIT_KEY) || 'F';
const unitListeners = new Set();
export const units = {
  get temp() { return tempUnit; },
  onChange(fn) { unitListeners.add(fn); },
};
export function initUnitToggle() {
  const btn = document.getElementById('unit-toggle');
  if (!btn) return;
  const render = () => { btn.textContent = `°${tempUnit}`; btn.title = `Showing °${tempUnit}; click for °${tempUnit === 'F' ? 'C' : 'F'}`; };
  render();
  btn.addEventListener('click', () => {
    tempUnit = tempUnit === 'F' ? 'C' : 'F';
    localStorage.setItem(UNIT_KEY, tempUnit);
    render();
    unitListeners.forEach((fn) => fn());
  });
}
export const toTemp = (c) => (c == null || !Number.isFinite(c) ? null : tempUnit === 'F' ? c * 1.8 + 32 : c);
export const tempDelta = (dc) => (tempUnit === 'F' ? dc * 1.8 : dc);
export const fmtTemp = (c, digits = 0) => (c == null || !Number.isFinite(c) ? '–' : `${toTemp(c).toFixed(digits)}°${tempUnit}`);

// ---------- numbers and times ----------
export function fmtFlow(v, withUnit = true) {
  if (v == null || !Number.isFinite(v)) return '–';
  const s = v >= 100 ? d3.format(',.3~r')(v) : v >= 10 ? d3.format('.3~r')(v) : d3.format('.2~r')(v);
  return withUnit ? `${s} ft³/s` : s;
}
export const fmtPct = (x, digits = 0) => (x == null || !Number.isFinite(x) ? '–' : `${(x * 100).toFixed(digits)}%`);
export const fmtSigned = (x) => (x == null ? '–' : `${x >= 0 ? '+' : '−'}${Math.abs(x * 100).toFixed(0)}%`);

const dtf = (opts) => new Intl.DateTimeFormat('en-US', { timeZone: TZ, ...opts });
const fDay = dtf({ weekday: 'short', month: 'short', day: 'numeric' });
const fDayYear = dtf({ month: 'short', day: 'numeric', year: 'numeric' });
const fDayTime = dtf({ weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const fFull = dtf({ weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
const fTime = dtf({ hour: 'numeric', minute: '2-digit' });
const fMonthYear = dtf({ month: 'short', year: 'numeric' });
export const fmt = {
  day: (t) => fDay.format(t),
  dayYear: (t) => fDayYear.format(t),
  dayTime: (t) => fDayTime.format(t),
  full: (t) => fFull.format(t),
  time: (t) => fTime.format(t),
  monthYear: (t) => fMonthYear.format(t),
};
export function ago(t) {
  const m = Math.round((Date.now() - t) / 60000);
  if (m < 2) return 'just now';
  if (m < 90) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

// ---------- flow vs normal (USGS WaterWatch-style classes) ----------
export const PCT_CLASSES = [
  { max: 0.1, label: 'Much below normal', color: '#a5541b' },
  { max: 0.25, label: 'Below normal', color: '#e8a14a' },
  { max: 0.75, label: 'Normal', color: '#4caf6e' },
  { max: 0.9, label: 'Above normal', color: '#5aa2f0' },
  { max: 1.01, label: 'Much above normal', color: '#1c3f94' },
];
export const NO_DATA = '#b7c0c8';
export function pctClass(p) {
  if (p == null || !Number.isFinite(p)) return { label: 'No current data', color: NO_DATA };
  return PCT_CLASSES.find((c) => p < c.max);
}
export function pctBadge(p) {
  const c = pctClass(p);
  const pct = p == null ? '' : ` · ${ordinal(Math.round(p * 100))} percentile`;
  return `<span class="badge" title="Compared with the same date in 2000–2022"><span class="dot" style="background:${c.color}"></span>${c.label}${pct}</span>`;
}
export function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

export function dayOfYear(t) {
  const parts = dtf({ year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(t);
  const y = +parts.find((p) => p.type === 'year').value;
  const m = +parts.find((p) => p.type === 'month').value;
  const d = +parts.find((p) => p.type === 'day').value;
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(y, 0, 0)) / 86400000);
}

// ---------- page chrome ----------
export function renderNav(currentId) {
  const nav = document.getElementById('site-nav');
  if (!nav) return;
  nav.innerHTML = [`<a href="./" ${currentId ? '' : 'aria-current="page"'}>All rivers</a>`,
    ...FEATURED.map((s) => `<a href="${siteHref(s.id)}" ${s.id === currentId ? 'aria-current="page"' : ''}>${s.short}</a>`)].join('');
}

export function renderFooter(extra = '') {
  const el = document.getElementById('footer');
  if (!el) return;
  el.innerHTML = `
    <p><b>flowcast prototype.</b> Forecasts shown are replays from the model's validation years (Oct 2020 – Sep 2022); later years are held back as an untouched test set. Current conditions are provisional USGS data.</p>
    ${extra}
    <p>Data: USGS Water Data API and NLDI, NHDPlus V2, USACE National Inventory of Dams, NOAA GEFS and SNODAS, NWS MARFC (via IEM). Maps: <a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, terrain from AWS Terrain Tiles (Mapzen). Non-commercial.</p>`;
}

// ---------- tooltip ----------
export function tooltip() {
  const el = document.getElementById('tooltip');
  return {
    show(html, x, y) {
      el.innerHTML = html;
      el.hidden = false;
      const r = el.getBoundingClientRect();
      const left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x + 14));
      const top = y + r.height + 24 > window.innerHeight ? y - r.height - 12 : y + 14;
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
    },
    hide() { el.hidden = true; },
  };
}

export function onResize(el, fn) {
  let w = el.clientWidth;
  const ro = new ResizeObserver(() => {
    if (Math.abs(el.clientWidth - w) > 4) {
      w = el.clientWidth;
      fn();
    }
  });
  ro.observe(el);
}

export function segmented(el, onChange) {
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    el.querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
    onChange(b.dataset);
  });
}
