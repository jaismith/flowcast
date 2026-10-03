import { useEffect, useMemo, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from './PlotFigure.jsx';
import { C } from '../lib/palette.js';
import { forecastAt, fmt, normalBand, presets, replayIssues, waterYear, withGaps } from '../lib/data.js';
import { issueAtOrBefore } from '../lib/scenarios.js';
import { categoryAt, floodLevels, ratingFor } from '../lib/rating.js';

const MARGIN = { marginLeft: 58, marginRight: 20 };
const DAY = 86400000;

/**
 * `debug`: the time-travel panel is showing and carries the replay labelling, so the page renders as a user
 * would see it. Without the panel (any production build) the replay badge stays on the page.
 */
export default function Forecast({ data, at, debug, onIssue }) {
  const hc = data.hindcast;
  const list = useMemo(() => replayIssues(hc), [hc]);
  const chips = useMemo(() => {
    const base = presets(hc, list);
    return at ? [{ key: 'now', label: 'Now', idx: issueAtOrBefore(hc, at) }, ...base.slice(1)] : base;
  }, [hc, list, at]);
  const [idx, setIdx] = useState(chips[0].idx);
  const f = useMemo(() => forecastAt(data, idx), [data, idx]);
  const normal = useMemo(() => normalBand(data.clim, f.from, f.to), [data.clim, f]);
  useEffect(() => onIssue?.(f.issue), [f.issue, onIssue]);

  const pos = list.indexOf(idx);
  const step = (d) => setIdx(list[Math.min(list.length - 1, Math.max(0, pos + d))]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea, select')) return;
      if (e.key === 'ArrowLeft') step(-1);
      if (e.key === 'ArrowRight') step(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const levels = useMemo(() => floodLevels(data.meta), [data.meta]);
  const rating = useMemo(() => ratingFor(data.meta.id), [data.meta.id]);
  const ahead = f.fan.filter((r) => r.t > f.issue);
  const low = Math.min(...ahead.map((r) => r.q05));
  const high = Math.max(...ahead.map((r) => r.q95));
  const start = f.now ?? ahead[0].q50;
  const rising = f.peak.q50 > start * 1.15 && f.peak.t - f.issue > 12 * 3600 * 1000;
  const peakCat = rating && categoryAt(levels, rating.stage(f.peak.q50));
  const headline = rising
    ? `Rising to about ${fmt.cfs(f.peak.q50)} cfs by ${fmt.day(f.peak.t)}${peakCat ? ` (${peakCat.label.toLowerCase()} stage)` : ''}`
    : `Steady to falling, ${fmt.cfs(low)}–${fmt.cfs(Math.max(start, f.fan.at(-1).q95))} cfs over 7 days`;

  return (
    <section className="card p-5 sm:p-6">
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
        <div>
          <div className="eyebrow">7-day flow forecast</div>
          <h2 className="mt-1 text-xl font-semibold tracking-tight sm:text-2xl">{headline}</h2>
          <p className="mt-1 text-sm text-muted">
            Likely range {fmt.cfs(low)}–{fmt.cfs(high)} cfs.{' '}
            {f.obsPeak.v != null && (
              <>
                Actual peak was <span className="font-medium text-ink">{fmt.cfs(f.obsPeak.v)} cfs</span> on {fmt.day(f.obsPeak.t)}.
              </>
            )}
          </p>
        </div>
        <ReplayControls f={f} pos={pos} total={list.length} step={step} labelled={!debug} />
      </div>

      <div className="mt-4 flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <button
            key={c.key}
            onClick={() => setIdx(c.idx)}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
              c.idx === idx ? 'border-ink bg-ink text-white' : 'border-line bg-white text-muted hover:border-faint hover:text-ink'
            }`}
          >
            {c.label}
          </button>
        ))}
      </div>

      <Drivers f={f} />

      <div className="mt-4">
        <div className="mb-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted" style={{ paddingLeft: MARGIN.marginLeft }}>
          <span className="font-medium text-ink">Water in, per 6 h</span>
          <Swatch color="#9fbff2" label="Rain (observed)" />
          <Swatch color={C.rain} label="Rain" />
          <Swatch color={C.snow} label="Snow" />
          <Swatch color={C.melt} label="Snowmelt" />
        </div>
        <PlotFigure deps={[f]} build={(w) => waterPlot(f, w)} />
        <PlotFigure deps={[f, normal, levels]} build={(w) => flowPlot(f, normal, levels, w)} />
      </div>
      <Legend />
    </section>
  );
}

function ReplayControls({ f, pos, total, step, labelled }) {
  return (
    <div className="flex items-center gap-3">
      <div className="text-right">
        {labelled && <ReplayBadge issue={f.issue} />}
        <div className="mt-1 text-sm font-medium">Issued {fmt.when(f.issue)}</div>
      </div>
      <div className="flex gap-1">
        <IconButton label="Previous issue (←)" onClick={() => step(-1)} disabled={pos <= 0}>
          ‹
        </IconButton>
        <IconButton label="Next issue (→)" onClick={() => step(1)} disabled={pos >= total - 1}>
          ›
        </IconButton>
      </div>
    </div>
  );
}

export function ReplayBadge({ issue }) {
  return (
    <div>
      <div className="inline-flex items-center gap-1.5 rounded-full bg-sun/15 px-2.5 py-0.5 text-[11px] font-semibold tracking-wide text-[#8a5a00] uppercase">
        Replay · not a live forecast
      </div>
      <div className="text-xs text-muted">Validation year WY{waterYear(issue)} · held out from training</div>
    </div>
  );
}

function IconButton({ children, label, ...rest }) {
  return (
    <button
      title={label}
      aria-label={label}
      className="grid size-9 place-items-center rounded-full border border-line bg-white text-lg leading-none text-ink hover:border-faint disabled:opacity-30"
      {...rest}
    >
      {children}
    </button>
  );
}

function Drivers({ f }) {
  const items = [
    { key: 'rain', label: 'Rain', value: fmt.in(f.totals.rain), color: C.rain, note: 'GEFS, 7 days' },
    { key: 'snow', label: 'Snow', value: fmt.in(f.totals.snow), color: C.snow, note: 'water equivalent' },
    { key: 'melt', label: 'Snowmelt', value: fmt.in(f.totals.melt), color: C.melt, note: 'SNODAS, observed' },
    { key: 'pack', label: 'Snowpack at issue', value: fmt.in(f.sweIn), color: '#ffffff', note: 'water equivalent' },
  ];
  return (
    <div className="mt-5 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-4">
      {items.map((d) => (
        <div key={d.key} className="bg-white px-4 py-3">
          <div className="flex items-center gap-2 text-xs font-medium text-muted">
            <span className="size-2.5 rounded-sm ring-1 ring-black/10" style={{ background: d.color }} />
            {d.label}
          </div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums">{d.value}</div>
          <div className="text-[11px] text-faint">{d.note}</div>
        </div>
      ))}
    </div>
  );
}

function Swatch({ color, label }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="size-2.5 rounded-sm" style={{ background: color }} />
      {label}
    </span>
  );
}

function Legend() {
  const sw = (style) => <span className="inline-block h-2.5 w-5 rounded-sm" style={style} />;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-muted">
      <span className="flex items-center gap-1.5">{sw({ background: C.flow, height: 3 })}Forecast (median)</span>
      <span className="flex items-center gap-1.5">{sw({ background: C.flow, opacity: 0.35 })}50% range</span>
      <span className="flex items-center gap-1.5">{sw({ background: C.flow, opacity: 0.14 })}90% range</span>
      <span className="flex items-center gap-1.5">{sw({ background: C.ink, height: 2 })}Observed</span>
      <span className="flex items-center gap-1.5">
        {sw({ backgroundImage: `radial-gradient(circle, ${C.ink} 1px, transparent 1.4px)`, backgroundSize: '4px 4px', height: 4 })}
        What happened
      </span>
      <span className="flex items-center gap-1.5">{sw({ background: C.normal })}Normal for the date</span>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- plots

function waterPlot(f, width) {
  const rows = [
    ...f.pastRain.map((w) => ({ t0: w.t0, t1: w.t1, k: 'observed rain', v: w.rain })),
    ...f.water.flatMap((w) => [
      { t0: w.t0, t1: w.t1, k: 'melt', v: w.melt },
      { t0: w.t0, t1: w.t1, k: 'snow', v: w.snow },
      { t0: w.t0, t1: w.t1, k: 'rain', v: w.rain },
    ]),
  ];
  const days = [];
  for (let d = 0; d < 7; d++) {
    const bins = f.water.slice(d * 4, d * 4 + 4);
    const total = bins.reduce((s, w) => s + w.rain + w.snow + w.melt, 0);
    const top = Math.max(...bins.map((w) => w.rain + w.snow + w.melt));
    if (total >= 0.2) days.push({ t: new Date(f.issue.getTime() + (d + 0.5) * DAY), total, top });
  }
  const ymax = Math.max(0.12, ...f.water.map((w) => w.rain + w.snow + w.melt), ...f.pastRain.map((w) => w.rain));
  return Plot.plot({
    width,
    height: 92,
    ...MARGIN,
    marginTop: 4,
    marginBottom: 2,
    x: { domain: [f.from, f.to], axis: null },
    y: { domain: [0, ymax * 1.35], reverse: true, ticks: 2, tickSize: 0, tickFormat: (d) => (d ? `${d}″` : ''), label: null },
    color: { domain: ['observed rain', 'rain', 'snow', 'melt'], range: ['#9fbff2', C.rain, C.snow, C.melt] },
    style: { fontFamily: 'inherit', fontSize: '11px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.rectY(rows, { x1: 't0', x2: 't1', y: 'v', fill: 'k', insetLeft: 0.75, insetRight: 0.75 }),
      Plot.ruleY([0], { stroke: C.line }),
      Plot.ruleX([f.issue], { stroke: C.faint, strokeDasharray: '2,3' }),
      Plot.text(days, { x: 't', y: 'top', text: (d) => fmt.in(d.total), dy: 9, fill: C.ink, fontWeight: 600, fontSize: 11 }),
      Plot.tip(
        f.water,
        Plot.pointerX({
          x1: 't0',
          x2: 't1',
          y: (w) => w.rain + w.snow + w.melt,
          title: (w) => `${fmt.when(w.t0)}\nRain ${w.rain.toFixed(2)} in\nSnow ${w.snow.toFixed(2)} in\nMelt ${w.melt.toFixed(2)} in`,
          anchor: 'top',
        }),
      ),
    ],
  });
}

function flowPlot(f, normal, levels, width) {
  const before = withGaps(f.observed.filter((r) => !r.after));
  const after = withGaps(f.observed.filter((r) => r.after));
  const dataMax = Math.max(...f.fan.map((r) => r.q95 ?? 0), ...f.observed.map((r) => r.v ?? 0), ...normal.map((r) => r.p75 ?? 0));
  // Flood lines only once the river is within reach of them; otherwise they would flatten every normal week.
  const shown = levels.filter((l) => l.cfs != null && l.cfs <= dataMax * 1.6).filter((l, i, a) => i === 0 || a[i - 1].cfs <= dataMax);
  const ymax = Math.max(dataMax, ...shown.map((l) => l.cfs)) * 1.12;
  const ahead = f.fan.filter((r) => r.t > f.issue);
  return Plot.plot({
    width,
    height: Math.max(260, Math.min(380, width * 0.36)),
    ...MARGIN,
    marginTop: 18,
    marginBottom: 30,
    x: {
      domain: [f.from, f.to],
      ticks: 10,
      tickFormat: (d) => d.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', timeZone: 'America/New_York' }),
      label: null,
      tickSize: 0,
    },
    y: { domain: [0, ymax], grid: true, ticks: 5, tickSize: 0, label: 'cfs', labelAnchor: 'top', labelArrow: 'none', tickFormat: (d) => d.toLocaleString('en-US') },
    style: { fontFamily: 'inherit', fontSize: '11px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.areaY(normal, { x: 't', y1: 'p25', y2: 'p75', fill: C.normal, curve: 'basis' }),
      Plot.ruleY(shown, { y: 'cfs', stroke: 'color', strokeWidth: 1.5, strokeDasharray: '5,3' }),
      Plot.text(shown, {
        y: 'cfs',
        frameAnchor: 'right',
        textAnchor: 'end',
        dx: -2,
        dy: -7,
        text: (l) => `${l.label} · ${l.ft} ft`,
        fill: 'color',
        fontWeight: 600,
        fontSize: 10.5,
        stroke: 'white',
        strokeWidth: 3,
        paintOrder: 'stroke',
      }),
      Plot.areaY(f.fan, { x: 't', y1: 'q05', y2: 'q95', fill: C.flow, fillOpacity: 0.13, curve: 'monotone-x' }),
      Plot.areaY(f.fan, { x: 't', y1: 'q25', y2: 'q75', fill: C.flow, fillOpacity: 0.24, curve: 'monotone-x' }),
      Plot.lineY(after, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 1.6, strokeDasharray: '0.5,3.5', strokeLinecap: 'round' }),
      Plot.lineY(f.fan, { x: 't', y: 'q50', stroke: C.flow, strokeWidth: 2.5, curve: 'monotone-x' }),
      Plot.lineY(before, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 1.75 }),
      Plot.ruleX([f.issue], { stroke: C.muted, strokeDasharray: '2,3' }),
      Plot.text([f.issue], { x: (d) => d, frameAnchor: 'top', dy: -12, text: () => 'Issued', fill: C.muted, fontWeight: 600 }),
      Plot.dot([f.peak], { x: 't', y: 'q50', r: 4.5, fill: C.flow, stroke: 'white', strokeWidth: 1.5 }),
      Plot.text([f.peak], {
        x: 't',
        y: 'q50',
        text: (d) => `Peak ${fmt.cfs(d.q50)}`,
        dy: -12,
        fill: C.flow,
        fontWeight: 700,
        fontSize: 12,
        stroke: 'white',
        strokeWidth: 4,
        paintOrder: 'stroke',
      }),
      Plot.tip(
        ahead,
        Plot.pointerX({
          x: 't',
          y: 'q50',
          title: (r) =>
            `${fmt.when(r.t)}\nMedian ${fmt.cfs(r.q50)} cfs\n50%: ${fmt.cfs(r.q25)}–${fmt.cfs(r.q75)}\n90%: ${fmt.cfs(r.q05)}–${fmt.cfs(r.q95)}` +
            (r.obs != null ? `\nActual ${fmt.cfs(r.obs)} cfs` : ''),
        }),
      ),
    ],
  });
}
