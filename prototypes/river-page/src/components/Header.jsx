import { useEffect, useMemo, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from './PlotFigure.jsx';
import FloodBar from './FloodBar.jsx';
import { categoryAt, floodLevels, ratingFor } from '../lib/rating.js';
import { C } from '../lib/palette.js';
import { archivedGauge, flowClass, fmt, liveGauge, normalFor, withGaps } from '../lib/data.js';

const TONE = { low: 'bg-sun/20 text-ink', normal: 'bg-melt/20 text-ink', high: 'bg-rain/20 text-ink' };

export default function Header({ data, at }) {
  const { meta, clim, observed } = data;
  const [fetched, setFetched] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!at) liveGauge(meta.id).then(setFetched, () => setError(true));
  }, [meta.id, at]);
  const archived = useMemo(() => at && archivedGauge(observed, at), [observed, at]);
  const live = at ? archived : fetched;

  const flow = live?.flow;
  const cls = flow && flowClass(clim, flow.t, flow.v);
  const normal = normalFor(clim, at ?? flow?.t ?? new Date());
  const stale = flow && at && at - flow.t > 3 * 3600 * 1000;
  const dayAgo = flow && live.series.find((p) => p.t >= new Date(flow.t.getTime() - 86400000));
  const change = flow && dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;
  const stage = live?.stage;
  const levels = floodLevels(meta);
  const ft = stage?.v ?? ratingFor(meta.id)?.stage(flow?.v) ?? null;
  const cat = categoryAt(levels, ft);
  const levelNote =
    ft == null || !levels.length
      ? ''
      : cat
        ? `${cat.label} stage`
        : `${(levels[0].ft - ft).toFixed(1)} ft below action stage`;

  return (
    <header className="pt-8 pb-6 sm:pt-12">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="eyebrow flex items-center gap-2">
            <span className="text-flow">flowcast</span>
            <span className="text-line">/</span>
            <span>USGS {meta.id}</span>
          </div>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">{meta.river}</h1>
          <p className="mt-1 text-base text-muted">
            at {meta.place} · {fmt.int(meta.area_mi2)} mi² basin
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          <span className={`size-2 rounded-full ${live ? 'bg-melt' : error ? 'bg-alert' : 'bg-faint animate-pulse'}`} />
          {live ? `Live from USGS · ${fmt.when(flow?.t ?? at ?? new Date())}` : error ? 'Live data unavailable' : 'Loading live data…'}
        </div>
      </div>

      <div className="card mt-6 grid grid-cols-3 overflow-hidden md:grid-cols-[1.3fr_1fr_1fr_2.2fr]">
        <Stat label="Flow now" big value={flow ? fmt.cfs(flow.v) : '—'} unit="cfs">
          {cls && <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${TONE[cls.tone]}`}>{cls.label}</span>}
          {stale && <span className="text-xs text-muted">Last reading {fmt.day(flow.t)}</span>}
          {at && !flow && <span className="text-xs text-muted">No recent reading</span>}
        </Stat>
        <Stat label="Last 24 h" value={change == null ? '—' : `${change > 0 ? '↑' : change < 0 ? '↓' : '→'} ${Math.abs(Math.round(change * 100))}%`}>
          <span className="text-xs text-muted">{change == null ? '' : Math.abs(change) < 0.03 ? 'Steady' : change > 0 ? 'Rising' : 'Falling'}</span>
          {live?.temp && <span className="text-xs text-muted">Water {fmt.f(live.temp.v)}</span>}
        </Stat>
        <Stat label="River level" value={ft == null ? '—' : ft.toFixed(1)} unit="ft">
          <div className="mt-1 w-full">
            <FloodBar levels={levels} ft={ft} />
          </div>
          <span className="text-xs text-muted">{levelNote}</span>
        </Stat>
        <div className="col-span-3 border-t border-line px-5 pt-4 pb-3 md:col-span-1 md:border-t-0 md:border-l">
          <div className="eyebrow">Flow, past 7 days</div>
          {live?.series?.length ? (
            C.family === 'ascii' ? (
              <AsciiSpark series={live.series} clim={clim} />
            ) : (
              <PlotFigure className="mt-1" deps={[live]} build={(w) => spark(withGaps(live.series), clim, w)} />
            )
          ) : (
            <div className="h-14" />
          )}
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <span className="inline-block h-2.5 w-4 shrink-0 rounded-sm bg-normal" />
            Normal for {fmt.monthDay(at ?? new Date())}: {fmt.cfs(normal.p25)}–{fmt.cfs(normal.p75)} cfs
          </div>
        </div>
      </div>
    </header>
  );
}

function Stat({ label, value, unit, big, children }) {
  return (
    <div className="border-line px-4 py-4 not-first:border-l sm:px-5">
      <div className="eyebrow">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`${big ? 'text-3xl' : 'text-2xl'} font-semibold tracking-tight tabular-nums`}>{value}</span>
        {unit && <span className="text-sm text-muted">{unit}</span>}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">{children}</div>
    </div>
  );
}

const BLOCKS = '▁▂▃▄▅▆▇█';

/** Flow as block characters, one per ~4 hours, scaled between the week's normal range and its own extremes. */
function AsciiSpark({ series, clim }) {
  const n = 28;
  const t0 = series[0].t.getTime();
  const span = series.at(-1).t.getTime() - t0 || 1;
  const cells = Array.from({ length: n }, () => []);
  for (const p of series) cells[Math.min(n - 1, Math.floor(((p.t - t0) / span) * n))].push(p.v);
  const vals = cells.map((c) => (c.length ? c.reduce((s, v) => s + v, 0) / c.length : null));
  const normal = normalFor(clim, series.at(-1).t);
  const present = vals.filter((v) => v != null);
  const lo = Math.min(...present, normal.p25);
  const hi = Math.max(...present, normal.p75);
  const level = (v) => Math.round(((v - lo) / (hi - lo || 1)) * (BLOCKS.length - 1));
  const nLo = level(normal.p25);
  const nHi = level(normal.p75);
  return (
    <div className="mt-2 overflow-hidden font-mono text-[18px] leading-none tracking-[-0.06em] whitespace-nowrap">
      {vals.map((v, i) => {
        if (v == null) return <span key={i} className="text-faint">·</span>;
        const k = level(v);
        const inNormal = k >= nLo && k <= nHi;
        return (
          <span key={i} style={{ color: i === n - 1 ? C.flow : inNormal ? C.ink : C.muted }}>
            {BLOCKS[k]}
          </span>
        );
      })}
      <div className="mt-1 text-[11px] tracking-normal text-muted">
        {'└'}
        {'─'.repeat(9)} 7d {'─'.repeat(9)}
        {'┘'} now
      </div>
    </div>
  );
}

function spark(series, clim, width) {
  const step = Math.max(1, Math.floor(series.length / 200));
  const pts = series.filter((_, i) => i % step === 0 || i === series.length - 1);
  const band = pts.filter((_, i) => i % 8 === 0).map((p) => ({ t: p.t, ...normalFor(clim, p.t) }));
  const vals = [...pts.map((p) => p.v), ...band.map((b) => b.p75), ...band.map((b) => b.p25)];
  return Plot.plot({
    width,
    height: 64,
    margin: 4,
    marginRight: 6,
    x: { axis: null },
    y: { axis: null, domain: [Math.min(...vals) * 0.9, Math.max(...vals) * 1.05] },
    marks: [
      Plot.areaY(band, { x: 't', y1: 'p25', y2: 'p75', fill: C.normal, curve: 'basis' }),
      Plot.lineY(pts, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 1.5 }),
      Plot.dot([pts.at(-1)], { x: 't', y: 'v', r: 3, fill: C.ink }),
    ],
  });
}
