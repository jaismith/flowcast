import { useEffect, useMemo, useRef, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure, { TipHead, TipRow } from './PlotFigure.jsx';
import FloodBar from './FloodBar.jsx';
import Basin from './Basin.jsx';
import { C } from '../lib/palette.js';
import { flowForecast, fmt, gaugeNow, nextUpdate, normalBand, tempForecast, withGaps } from '../lib/data.js';
import { floodLevels } from '../lib/rating.js';
import { basinDriver, outlook, riverStatus } from '../lib/story.js';
import { watershedPhrase } from '../lib/basins.js';

const HOUR = 3600 * 1000;
/** "Callicoon, N.Y.": the page's one exception to two-letter state codes. */
export const placeName = (place) => place.replace(/, NY$/, ', N.Y.');
const DAY = 24 * HOUR;
const MARGIN = { marginLeft: 44, marginRight: 16 };
/** A forecast issued within this long reads as "Now" on the charts; older ones say "Issued". */
const FRESH = 3 * HOUR;
/**
 * Past this age the basin weather is shown as it was at the issue time (Open-Meteo's archive, which runs about
 * five days behind), to match an old forecast such as the dev fixtures'. Live forecasts get live weather.
 */
const STALE = 7 * DAY;

/**
 * One site's page (`SiteData` from site.ts), once it has a forecast; until then App shows WarmingUp. `updating` is
 * set while a newer forecast is on its way; `wakeStatus` is the lazy-forecast status from the visit, if one was sent.
 */
export default function Editorial({ site, updating, wakeStatus, layer }) {
  const meta = site;
  const { clim, geo } = site;
  const gauge = useMemo(() => gaugeNow(site), [site]);
  const f = useMemo(() => (site.forecast ? flowForecast(site) : null), [site]);
  const levels = useMemo(() => floodLevels(site.floods), [site.floods]);
  const normal = useMemo(() => f && normalBand(clim, f.from, f.to), [clim, f]);
  const basin = site.watershed;
  const sheds = watershedPhrase(basin);
  const status = useMemo(() => riverStatus({ clim, gauge, levels }), [clim, gauge, levels]);
  const next = useMemo(() => (f ? outlook({ f, levels }) : WAITING), [f, levels]);
  const tf = useMemo(() => (f && site.forecast.temp ? tempForecast(site, f.issue) : null), [site, f]);
  const views = tf ? ['flow', 'temp'] : ['flow'];
  const [picked, setPicked] = useState(() => sessionStorage.getItem('river-view') ?? 'flow');
  const view = views.includes(picked) ? picked : 'flow';
  const setView = (v) => {
    sessionStorage.setItem('river-view', v);
    setPicked(v);
  };
  const age = f ? Date.now() - f.issue.getTime() : null;
  const isNow = age != null && age < FRESH;
  const weatherAt = age != null && age > STALE ? f.issue : null;
  const driver = useMemo(() => f && basinDriver({ view, f, tf, next }), [view, f, tf, next]);

  return (
    <div className="mx-auto max-w-5xl px-5 pb-20 sm:px-8">
      <header className="pt-10 sm:pt-14">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {meta.river} <span className="font-normal text-muted">at {placeName(meta.place)}</span>
          </h1>
          <p className="text-[13px] text-muted">
            {gauge.flow ? `${gauge.stale ? 'Last reading' : 'Updated'} ${fmt.whenYear(gauge.flow.t)}` : 'No recent gauge reading'} · USGS {meta.id.replace('USGS-', '')}
          </p>
        </div>
        <Glance status={status} next={next} nextTitle="Next 7 days" gauge={gauge} levels={levels} />
      </header>

      {f ? (
        <section className="mt-12">
          <h2 className="text-lg font-semibold">
            <ViewPicker view={view} views={views} setView={setView} /> at {meta.short}
          </h2>
          <p className="mt-1 text-sm text-muted">{VIEWS[view].dek}</p>
          {/* Both views stay mounted in one grid cell at the same height, so switching cross-fades without moving
              anything below (a freshly mounted chart is empty for a frame while it measures its width). */}
          <div className="mt-5 grid">
            <Fade show={view === 'flow'}>
              <div className="mb-1 flex h-[18px] items-baseline gap-2 text-[12px] text-muted" style={{ paddingLeft: MARGIN.marginLeft }}>
                <span className="size-2 translate-y-px rounded-[2px]" style={{ background: C.rain }} />
                Rain and snowmelt
                <span className="font-semibold text-ink">{fmt.in(f.totals.rain + f.totals.snow + f.totals.melt)}</span>
                over the next 7 days
              </div>
              <PlotFigure deps={[f]} build={(w) => waterStrip(f, w)} tip={stripTip} />
              <PlotFigure deps={[f, normal, levels, isNow]} tip={flowTip} build={(w) => flowChart({ f, normal, levels, isNow }, w)} />
            </Fade>
            {tf && (
              <Fade show={view === 'temp'}>
                <PlotFigure deps={[tf, isNow]} tip={tempTip} build={(w) => tempChart({ tf, isNow, extra: STRIP_BLOCK }, w)} />
              </Fade>
            )}
          </div>
          <div className="mt-3 border-t border-line pt-3 text-[13px] text-muted">
            Forecast issued {fmt.whenYear(f.issue)}
            {updating ? ' · a newer one is on its way' : <NextUpdate issue={f.issue} status={wakeStatus ?? site.status} />}
          </div>
        </section>
      ) : null}

      {f && <Drivers f={f} />}

      <section className="mt-16">
        <h2 className="text-lg font-semibold">The watershed</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          {sheds ? (
            <span title={`${basin.level} ${basin.parts.map((p) => p.huc).join(', ')} · ${basin.source}`}>{sheds[0].toUpperCase() + sheds.slice(1)}: </span>
          ) : null}
          {fmt.int(meta.area_mi2)} square miles upstream of the gauge.
          {meta.travel_time_max_h != null && ` Water from the headwaters takes up to ${Math.round(meta.travel_time_max_h / 24)} days to reach ${meta.short}.`}
        </p>
        {geo && (
          <div className="mt-5">
            <Basin meta={meta} geo={geo} at={weatherAt} initialLayer={layer} suggested={driver} />
          </div>
        )}
      </section>

      <About meta={meta} levels={levels} basin={basin} />

      <footer className="mt-16 max-w-3xl border-t border-line pt-4 text-xs leading-relaxed text-faint">
        Flow forecasts are flowcast’s three-seed LSTM ensemble (132 samples, calibrated). Water temperature is flowcast’s two-seed temperature model (88
        samples). Snowmelt is a SNOW-17 estimate. Sources: USGS Water Data API and NLDI, NWS flood stages, NOAA GEFS, MRMS and SNODAS, Open-Meteo, USACE
        NID. Basemap © OpenFreeMap, OpenStreetMap contributors.
      </footer>
    </div>
  );
}

/** The forecast slot while a site has no forecast yet. */
const WAITING = { label: 'No forecast yet', color: C.faint, value: null, detail: '' };

const MINUTE = 60 * 1000;

/** " · Next update ~3:30 PM" for an active site, " · Update delayed" once it's overdue, nothing while snoozed or paused. */
function NextUpdate({ issue, status }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), MINUTE);
    return () => clearInterval(t);
  }, []);
  if (status === 'snoozed' || status === 'paused' || status === 'waking') return null;
  const next = nextUpdate(issue, now);
  return status === 'delayed' || next.late ? ' · Update delayed' : ` · Next update ~${fmt.localTime(next.at, new Date(now))}`;
}

const VIEWS = {
  flow: { label: 'Flow', dek: 'The past three days and the seven-day forecast, with the National Weather Service’s flood stages shown as flows.' },
  temp: { label: 'Water temperature', dek: 'The past three days and the hourly forecast for the week.' },
};

/** The rain caption (18 px + 4 px margin) and strip (64 px) the flow view has above its chart. */
const STRIP_BLOCK = 86;
const chartHeight = (width) => Math.max(300, Math.min(440, width * 0.42));

function Fade({ show, children }) {
  return (
    <div
      className={`col-start-1 row-start-1 transition-[opacity,translate] duration-200 ease-out ${show ? 'translate-y-0 opacity-100' : 'pointer-events-none translate-y-1 opacity-0'}`}
      aria-hidden={!show}
    >
      {children}
    </div>
  );
}

/** The heading's first word, which switches the chart below (plain text when the site has one view). */
function ViewPicker({ view, views, setView }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => !ref.current.contains(e.target) && setOpen(false);
    const onKey = (e) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  if (views.length < 2) return VIEWS[view].label;
  return (
    <span ref={ref} className="relative inline-block">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="-mx-1 inline-flex items-center gap-1 rounded-md px-1 underline decoration-faint decoration-dotted underline-offset-4 transition-colors hover:bg-ink/[0.05]"
      >
        {VIEWS[view].label}
        <svg viewBox="0 0 12 12" className={`size-3 text-muted transition-transform duration-150 ${open ? 'rotate-180' : ''}`} aria-hidden>
          <path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <ul
        role="listbox"
        className={`absolute top-full left-0 z-20 mt-1.5 min-w-52 origin-top-left rounded-lg bg-card p-1 text-sm font-normal shadow-lg ring-1 ring-line transition duration-150 ease-out ${
          open ? 'scale-100 opacity-100' : 'pointer-events-none scale-95 opacity-0'
        }`}
      >
        {views.map((k) => [k, VIEWS[k]]).map(([k, v]) => (
          <li key={k}>
            <button
              role="option"
              aria-selected={view === k}
              tabIndex={open ? 0 : -1}
              onClick={() => {
                setView(k);
                setOpen(false);
              }}
              className={`flex w-full items-center justify-between gap-6 rounded-md px-2.5 py-1.5 text-left hover:bg-ink/[0.05] ${view === k ? 'font-medium text-ink' : 'text-muted'}`}
            >
              {v.label}
              {view === k && (
                <svg viewBox="0 0 12 12" className="size-3" aria-hidden>
                  <path d="M2.5 6.2 5 8.5l4.5-5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </button>
          </li>
        ))}
      </ul>
    </span>
  );
}

/** Same three slots, same order, every visit: now, the week ahead, and the gauge details. */
function Glance({ status, next, nextTitle, gauge, levels }) {
  const d = status?.dPct;
  const trend =
    d == null ? '' : Math.abs(d) < 0.03 ? 'Steady over 24 hours' : `${d > 0 ? '↑' : '↓'} ${d >= 1 ? `${(1 + d).toFixed(1)}×` : `${Math.abs(Math.round(d * 100))}%`} in 24 hours`;
  const ft = status?.ft;
  return (
    <div className="mt-6 grid gap-x-12 gap-y-8 border-t border-line pt-6 md:grid-cols-[1fr_1fr_1.1fr]">
      <Slot title="Now" label={status?.label ?? 'No recent reading'} color={status?.color} value={status ? fmt.cfs(status.flow.v) : '—'} detail={trend} />
      <Slot title={nextTitle} label={next.label} color={next.color} value={fmt.cfs(next.value)} caption={next.caption} detail={next.detail} />
      <div>
        <div className="text-[13px] text-muted">River level</div>
        <div className="mt-1 flex items-baseline gap-3">
          <span className="text-2xl font-semibold tracking-tight tabular-nums">{ft == null ? '—' : `${ft.toFixed(1)} ft`}</span>
          <span className="text-[13px] text-muted">
            {status?.cat ? status.label : levels[0] && ft != null ? `${(levels[0].ft - ft).toFixed(1)} ft below action` : ''}
          </span>
        </div>
        <div className="mt-2 max-w-64">
          <FloodBar levels={levels} ft={ft} />
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 text-[13px]">
          <div>
            <dt className="text-muted">Water</dt>
            <dd className="font-medium tabular-nums">{gauge?.temp ? fmt.f(gauge.temp.v) : '—'}</dd>
          </div>
          <div>
            <dt className="text-muted">Normal flow today</dt>
            <dd className="font-medium tabular-nums">{status?.normal.p25 != null ? `${fmt.cfs(status.normal.p25)}–${fmt.cfs(status.normal.p75)}` : '—'}</dd>
          </div>
        </dl>
      </div>
    </div>
  );
}

function Slot({ title, label, color, value, caption, detail }) {
  return (
    <div>
      <div className="text-[13px] text-muted">{title}</div>
      <div className="mt-2 flex items-center gap-2 text-[15px] font-semibold">
        <span className="size-3 rounded-full" style={{ background: color ?? C.faint }} />
        {label ?? '…'}
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        {caption && <span className="text-[13px] whitespace-nowrap text-muted">{caption}</span>}
        <span className="text-[2.6rem] leading-none font-semibold tracking-tight tabular-nums">{value}</span>
        <span className="text-base text-muted">cfs</span>
      </div>
      <div className="mt-2 text-[13px] text-muted">{detail}</div>
    </div>
  );
}

function Drivers({ f }) {
  const t = f.totals;
  const parts = [];
  const day = (bins) => {
    const best = bins.reduce((m, w) => (w.rain + w.snow > m.rain + m.snow ? w : m), bins[0]);
    return best.t0.toLocaleDateString('en-US', { weekday: 'long', timeZone: 'America/New_York' });
  };
  if (t.rain >= 0.05) parts.push(<span key="r"><Word color={C.rain}>{fmt.in(t.rain)} of rain</Word> is in the forecast, most of it on {day(f.water)}</span>);
  else parts.push(<span key="r">Little or no rain is in the forecast</span>);
  if (t.snow >= 0.05) parts.push(<span key="s">, with <Word color={C.snow}>{fmt.in(t.snow)} falling as snow</Word></span>);
  parts.push(<span key="p">. </span>);
  if (f.sweIn >= 0.1) {
    parts.push(
      <span key="m">
        The snowpack holds <Word color={C.snow}>{fmt.in(f.sweIn)} of water</Word>
        {t.melt >= 0.05 ? (
          <>
            , and <Word color={C.melt}>{fmt.in(t.melt)} melted</Word> over the week
          </>
        ) : (
          ', and little of it melted over the week'
        )}
        .
      </span>,
    );
  } else parts.push(<span key="m">There is no snow on the ground.</span>);
  return (
    <section className="mt-12 max-w-3xl">
      <h2 className="text-lg font-semibold">What’s driving it</h2>
      <p className="mt-2 text-base leading-relaxed text-ink/85">{parts}</p>
    </section>
  );
}

function Word({ color, children }) {
  return (
    <span className="font-semibold" style={{ color, textDecoration: `underline 2px ${color}55`, textUnderlineOffset: 4 }}>
      {children}
    </span>
  );
}

function About({ meta, levels, basin }) {
  const rows = [
    ['USGS gauge', <a key="g" className="underline decoration-line underline-offset-2 hover:decoration-ink" href={meta.usgs_url} target="_blank" rel="noreferrer">{meta.id.replace('USGS-', '')}</a>],
    ['Watershed', basin ? `${basin.name} (${basin.level} ${basin.huc})` : '—'],
    ['NWS forecast point', meta.nws_lid ?? '—'],
    ['Drainage area', `${fmt.int(meta.area_mi2)} mi²`],
    ['Typical flow', meta.median_flow_cfs != null ? `${fmt.cfs(meta.median_flow_cfs)} cfs (median)` : '—'],
    ['Forest', meta.forest_frac != null ? fmt.pct(meta.forest_frac) : '—'],
    ['Precipitation as snow', meta.snow_frac != null ? fmt.pct(meta.snow_frac) : '—'],
    ['Major dams upstream', meta.nid_major_dams == null ? '—' : meta.nid_dams != null ? `${meta.nid_major_dams} of ${meta.nid_dams}` : `${meta.nid_major_dams}`],
    ['Location', `${meta.lat.toFixed(3)}°N, ${Math.abs(meta.lon).toFixed(3)}°W`],
  ];
  return (
    <section className="mt-16">
      <h2 className="text-lg font-semibold">About this gauge</h2>
      <div className="mt-4 grid gap-x-12 gap-y-8 lg:grid-cols-[1.4fr_1fr]">
        <dl className="grid grid-cols-2 gap-x-8 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-3 border-b border-line py-2">
              <dt className="text-muted">{k}</dt>
              <dd className="text-right font-medium tabular-nums">{v}</dd>
            </div>
          ))}
        </dl>
        <div className="text-sm">
          <div className="text-muted">Flood stages</div>
          <ul className="mt-2">
            {[...levels].reverse().map((l) => (
              <li key={l.key} className="flex items-baseline justify-between gap-3 border-b border-line py-2">
                <span className="flex items-center gap-2">
                  <span className="size-2.5 rounded-sm" style={{ background: l.color }} />
                  {l.label}
                </span>
                <span className="tabular-nums">
                  <span className="font-medium">{l.ft} ft</span>
                  {l.cfs != null && <span className="text-muted"> · {fmt.cfs(l.cfs)} cfs</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------- charts

function waterStrip(f, width) {
  const rows = [
    ...f.pastRain.map((w) => ({ t0: w.t0, t1: w.t1, k: 'past', v: w.rain })),
    ...f.water.flatMap((w) => [
      { t0: w.t0, t1: w.t1, k: 'melt', v: w.melt },
      { t0: w.t0, t1: w.t1, k: 'snow', v: w.snow },
      { t0: w.t0, t1: w.t1, k: 'rain', v: w.rain },
    ]),
  ];
  const ymax = Math.max(0.15, ...f.water.map((w) => w.rain + w.snow + w.melt), ...f.pastRain.map((w) => w.rain));
  return Plot.plot({
    width,
    height: 64,
    ...MARGIN,
    marginTop: 2,
    marginBottom: 0,
    x: { domain: [f.from, f.to], axis: null },
    y: { domain: [0, ymax * 1.1], reverse: true, axis: null },
    color: { domain: ['past', 'rain', 'snow', 'melt'], range: [C.rain, C.rain, C.snow, C.melt] },
    style: { fontFamily: 'inherit', fontSize: '12px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.rectY(rows, { x1: 't0', x2: 't1', y: 'v', fill: 'k', fillOpacity: (d) => (d.k === 'past' ? 0.45 : 0.9), insetLeft: 0.5, insetRight: 0.5 }),
      Plot.ruleY([0], { stroke: C.line }),
      Plot.ruleX(stripHover(f), Plot.pointerX({ x: mid, stroke: C.ink, strokeOpacity: 0.12, strokeWidth: 8 })),
    ],
  });
}

const mid = (w) => new Date((w.t0.getTime() + w.t1.getTime()) / 2);
const hourOf = (d) => d.toLocaleTimeString('en-US', { hour: 'numeric', timeZone: 'America/New_York' });
const stripHover = (f) => [...f.pastRain.map((w) => ({ ...w, snow: 0, melt: 0, past: true })), ...f.water];

const stripTip = {
  at: (w) => [mid(w), w.rain + w.snow + w.melt],
  render: (w) => (
    <>
      <TipHead>
        {fmt.day(w.t0)}, {hourOf(w.t0)} – {hourOf(w.t1)}
      </TipHead>
      <TipRow swatch={C.rain} label={w.past ? 'Rain (observed)' : 'Rain'} value={`${w.rain.toFixed(2)} in`} />
      {w.snow >= 0.005 && <TipRow swatch={C.snow} label="Snow (as water)" value={`${w.snow.toFixed(2)} in`} />}
      {w.melt >= 0.005 && <TipRow swatch={C.melt} label="Snowmelt" value={`${w.melt.toFixed(2)} in`} />}
    </>
  ),
};

const flowTip = {
  at: (r) => [r.t, r.past ? r.v : r.q50],
  render: (r) => (
    <>
      <TipHead>
        {fmt.day(r.t)}, {hourOf(r.t)}
      </TipHead>
      {r.past ? (
        <TipRow swatch={C.ink} label="Observed" value={`${fmt.cfs(r.v)} cfs`} />
      ) : (
        <>
          <TipRow swatch={C.flow} label="Forecast" value={`${fmt.cfs(r.q50)} cfs`} />
          <TipRow swatch={`${C.flow}55`} label="Likely" value={`${fmt.cfs(r.q25)}–${fmt.cfs(r.q75)}`} />
          {r.obs != null && <TipRow swatch={C.ink} dotted label="What happened" value={`${fmt.cfs(r.obs)} cfs`} />}
        </>
      )}
    </>
  ),
};

const deg = (v) => `${Math.round(v)}°F`;

const tempTip = {
  at: (r) => [r.t, r.past ? r.v : r.q50],
  render: (r) => (
    <>
      <TipHead>
        {fmt.day(r.t)}, {hourOf(r.t)}
      </TipHead>
      {r.past ? (
        <TipRow swatch={C.ink} label="Observed" value={deg(r.v)} />
      ) : (
        <>
          <TipRow swatch={C.flow} label="Forecast" value={deg(r.q50)} />
          <TipRow swatch={`${C.flow}55`} label="Likely" value={`${Math.round(r.q25)}–${deg(r.q75)}`} />
          {r.obs != null && <TipRow swatch={C.ink} dotted label="What happened" value={deg(r.obs)} />}
        </>
      )}
    </>
  ),
};

function tempChart({ tf, isNow, extra = 0 }, width) {
  const before = withGaps(tf.observed.filter((r) => !r.after));
  const after = withGaps(tf.observed.filter((r) => r.after));
  const vals = [...tf.fan.flatMap((r) => [r.q05, r.q95]), ...tf.observed.map((r) => r.v), ...tf.normal.flatMap((r) => [r.lo, r.hi])].filter((v) => v != null);
  const ymin = Math.floor(Math.min(...vals) / 5) * 5 - 2;
  const ymax = Math.ceil(Math.max(...vals) / 5) * 5 + 2;
  // The week's high is the peak of the drawn median line, so the label always sits on it; none in a week that
  // only cools from the start, where it would sit on the issue point.
  const peak = tf.fan.filter((r) => r.t > tf.issue).reduce((m, r) => (r.q50 > (m?.q50 ?? -Infinity) ? r : m), null);
  const hottest = peak && peak.t - tf.issue > 6 * HOUR ? peak : null;
  const nowPt = tf.fan[0]?.t.getTime() === tf.issue.getTime() ? tf.fan[0] : null;
  const lastHourly = tf.fan.at(-1);
  const halo = { stroke: C.paper, strokeWidth: 4, paintOrder: 'stroke' };

  return Plot.plot({
    width,
    height: chartHeight(width) + extra,
    ...MARGIN,
    marginTop: 14,
    marginBottom: 30,
    x: {
      domain: [tf.from, tf.to],
      ticks: 'day',
      tickFormat: (d) => d.toLocaleDateString('en-US', { weekday: 'short', timeZone: 'America/New_York' }),
      tickSize: 0,
      label: null,
    },
    y: { domain: [ymin, ymax], ticks: 5, tickSize: 0, tickFormat: (d) => `${d}°`, grid: true, label: null },
    style: { fontFamily: 'inherit', fontSize: '12px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.areaY(tf.normal, { x: 't', y1: 'lo', y2: 'hi', fill: C.normal, curve: 'basis' }),
      Plot.text(tf.normal.length ? [tf.normal[Math.floor(tf.normal.length * 0.12)]] : [], {
        x: 't',
        y: (d) => (d.lo + d.hi) / 2,
        text: () => 'Normal for the date',
        fill: C.muted,
        fontSize: 11,
        stroke: C.normal,
        strokeWidth: 3,
        paintOrder: 'stroke',
      }),
      Plot.areaY(tf.fan, { x: 't', y1: 'q05', y2: 'q95', fill: C.flow, fillOpacity: 0.1, curve: 'monotone-x' }),
      Plot.areaY(tf.fan, { x: 't', y1: 'q25', y2: 'q75', fill: C.flow, fillOpacity: 0.2, curve: 'monotone-x' }),
      Plot.ruleX([tf.issue], { stroke: C.ink, strokeOpacity: 0.35, strokeDasharray: '2,3' }),
      Plot.lineY(after, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 1.5, strokeDasharray: '1,3.5', strokeLinecap: 'round' }),
      Plot.lineY(tf.fan, { x: 't', y: 'q50', stroke: C.flow, strokeWidth: 2.5, curve: 'monotone-x' }),
      Plot.lineY(before, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 2 }),
      lastHourly ? Plot.text([lastHourly], { x: 't', y: 'q05', text: () => 'Hourly forecast', textAnchor: 'end', dy: 14, fill: C.flow, fontWeight: 600, ...halo }) : null,
      hottest ? Plot.dot([hottest], { x: 't', y: 'q50', r: 4, fill: C.flow, stroke: C.paper, strokeWidth: 2 }) : null,
      hottest ? Plot.text([hottest], { x: 't', y: 'q95', text: (d) => `High ${deg(d.q50)}`, dy: -12, fill: C.flow, fontWeight: 600, ...halo }) : null,
      nowPt ? Plot.dot([nowPt], { x: 't', y: 'q50', r: 4.5, fill: C.ink, stroke: C.paper, strokeWidth: 2 }) : null,
      nowPt
        ? Plot.text([nowPt], { x: 't', y: 'q50', text: (d) => `${isNow ? 'Now' : 'Issued'} ${deg(d.q50)}`, textAnchor: 'end', dx: -8, dy: -10, fill: C.ink, fontWeight: 600, ...halo })
        : null,
      Plot.ruleX(
        [...before.filter((r) => r.v != null).map((r) => ({ ...r, past: true })), ...tf.fan.slice(1)],
        Plot.pointerX({ x: 't', stroke: C.ink, strokeOpacity: 0.25 }),
      ),
    ].filter(Boolean),
  });
}

const kcfs = (v) => (v >= 10000 ? `${Math.round(v / 1000)}k` : v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : `${Math.round(v)}`);

function flowChart({ f, normal, levels, isNow }, width) {
  const fan = f.fan;
  const before = withGaps(f.observed.filter((r) => !r.after));
  const after = withGaps(f.observed.filter((r) => r.after));
  const dataMax = Math.max(...fan.map((r) => r.q95 ?? 0), ...f.observed.map((r) => r.v ?? 0), ...normal.map((r) => r.p75 ?? 0));
  // Flood bands only when the river gets within reach of action stage; otherwise they would flatten an ordinary week.
  const withCfs = levels.filter((l) => l.cfs != null);
  const reach = withCfs[0] && dataMax >= withCfs[0].cfs * 0.45;
  const next = withCfs.find((l) => l.cfs > dataMax) ?? withCfs.at(-1);
  const ymax = reach ? Math.max(dataMax * 1.08, next.cfs * 1.1) : dataMax * 1.15;
  const bands = reach ? withCfs.map((l, i) => ({ ...l, y1: l.cfs, y2: Math.min(withCfs[i + 1]?.cfs ?? ymax, ymax) })).filter((b) => b.y1 < ymax) : [];

  const ahead = fan.filter((r) => r.t > f.issue);
  const crest = ahead.reduce((m, r) => (r.q50 > m.q50 ? r : m), ahead[0]);
  const showCrest = crest.t - f.issue > 6 * HOUR && crest.q50 > (fan[0]?.q50 ?? 0) * 1.05;
  const obsAfter = after.filter((r) => r.v != null);
  const actual = obsAfter.reduce((m, r) => (r.v > m.v ? r : m), obsAfter[0] ?? { v: null });
  const nowPt = fan[0]?.t.getTime() === f.issue.getTime() ? fan[0] : null;
  // Line labels sit on the lines, late in the week where the forecast has usually settled and away from the crest.
  const atDay = (d) => ahead.reduce((m, r) => (Math.abs(r.t - f.issue - d * DAY) < Math.abs(m.t - f.issue - d * DAY) ? r : m), ahead[0]);
  const clear = (r) => !showCrest || Math.abs(r.t - crest.t) > 1.4 * DAY;
  const labelAt = [5.5, 3, 1.5, 6.5].map(atDay).find(clear) ?? atDay(5.5);
  const rangeAt = [4, 2, 6].map(atDay).find((r) => clear(r) && Math.abs(r.t - labelAt.t) > DAY) ?? atDay(4);
  const halo = { stroke: C.paper, strokeWidth: 4, paintOrder: 'stroke' };

  return Plot.plot({
    width,
    height: chartHeight(width),
    ...MARGIN,
    marginTop: 8,
    marginBottom: 30,
    x: {
      domain: [f.from, f.to],
      ticks: 'day',
      tickFormat: (d) => d.toLocaleDateString('en-US', { weekday: 'short', timeZone: 'America/New_York' }),
      tickSize: 0,
      label: null,
    },
    y: { domain: [0, ymax], ticks: 5, tickSize: 0, tickFormat: kcfs, grid: true, label: null },
    style: { fontFamily: 'inherit', fontSize: '12px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.rect(bands, { x1: f.from, x2: f.to, y1: 'y1', y2: 'y2', fill: 'color', fillOpacity: 0.08 }),
      Plot.ruleY(bands, { y: 'y1', stroke: 'color', strokeOpacity: 0.8 }),
      Plot.text(bands, {
        x: f.to,
        y: 'y1',
        text: (b) => `${b.label} · ${kcfs(b.cfs)}`,
        textAnchor: 'end',
        dx: -6,
        dy: -8,
        fill: 'color',
        fontWeight: 600,
        fontSize: 11.5,
        ...halo,
      }),
      !reach && withCfs[0]
        ? Plot.text([`Action stage (${fmt.cfs(withCfs[0].cfs)} cfs) is off the chart ↑`], { frameAnchor: 'top-right', textAnchor: 'end', dx: -6, dy: 6, fill: C.muted, fontSize: 11 })
        : null,
      Plot.areaY(normal, { x: 't', y1: 'p25', y2: 'p75', fill: C.normal, curve: 'basis' }),
      Plot.text(normal.length ? [normal[Math.floor(normal.length * 0.12)]] : [], { x: 't', y: (d) => (d.p25 + d.p75) / 2, text: () => 'Normal for the date', fill: C.muted, fontSize: 11, stroke: C.normal, strokeWidth: 3, paintOrder: 'stroke' }),
      Plot.areaY(fan, { x: 't', y1: 'q05', y2: 'q95', fill: C.flow, fillOpacity: 0.1, curve: 'monotone-x' }),
      Plot.areaY(fan, { x: 't', y1: 'q25', y2: 'q75', fill: C.flow, fillOpacity: 0.2, curve: 'monotone-x' }),
      Plot.ruleX([f.issue], { stroke: C.ink, strokeOpacity: 0.35, strokeDasharray: '2,3' }),
      Plot.lineY(after, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 1.5, strokeDasharray: '1,3.5', strokeLinecap: 'round' }),
      Plot.lineY(fan, { x: 't', y: 'q50', stroke: C.flow, strokeWidth: 2.5, curve: 'monotone-x' }),
      Plot.lineY(before, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 2 }),
      Plot.text([labelAt], { x: 't', y: 'q50', text: () => 'Forecast', dy: -10, fill: C.flow, fontWeight: 600, ...halo }),
      Plot.text([rangeAt], { x: 't', y: 'q95', text: () => 'Likely range', dy: -7, fill: C.flow, fontSize: 11, ...halo }),
      nowPt ? Plot.dot([nowPt], { x: 't', y: 'q50', r: 4.5, fill: C.ink, stroke: C.paper, strokeWidth: 2 }) : null,
      nowPt
        ? Plot.text([nowPt], { x: 't', y: 'q50', text: (d) => `${isNow ? 'Now' : 'Issued'} ${fmt.cfs(d.q50)}`, textAnchor: 'end', dx: -8, dy: -10, fill: C.ink, fontWeight: 600, ...halo })
        : null,
      showCrest ? Plot.dot([crest], { x: 't', y: 'q50', r: 4, fill: C.flow, stroke: C.paper, strokeWidth: 2 }) : null,
      showCrest
        ? Plot.text([crest], {
            x: 't',
            y: 'q50',
            text: (d) => `Forecast crest ${fmt.cfs(d.q50)} cfs\n${fmt.when(d.t)}`,
            dy: -22,
            lineHeight: 1.25,
            fill: C.flow,
            fontWeight: 600,
            ...halo,
          })
        : null,
      actual.v != null && actual.v > (crest?.q50 ?? 0) * 1.1
        ? Plot.text([actual], { x: 't', y: 'v', text: (d) => `What happened: ${fmt.cfs(d.v)} cfs`, textAnchor: 'start', dx: 8, dy: -4, fill: C.ink, fontSize: 11, ...halo })
        : null,
      Plot.ruleX(
        [...before.filter((r) => r.v != null).map((r) => ({ ...r, past: true })), ...ahead],
        Plot.pointerX({ x: 't', stroke: C.ink, strokeOpacity: 0.25 }),
      ),
    ].filter(Boolean),
  });
}

