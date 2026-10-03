import { useEffect, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from './PlotFigure.jsx';
import { C } from '../lib/palette.js';
import { flowClass, fmt, liveGauge, normalFor } from '../lib/data.js';

const TONE = { low: 'bg-sun/15 text-[#8a5a00]', normal: 'bg-melt/12 text-[#17695f]', high: 'bg-rain/12 text-[#1f55b0]' };

export default function Header({ meta, clim }) {
  const [live, setLive] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    liveGauge(meta.id).then(setLive, () => setError(true));
  }, [meta.id]);

  const flow = live?.flow;
  const cls = flow && flowClass(clim, flow.t, flow.v);
  const normal = flow && normalFor(clim, flow.t);
  const dayAgo = flow && live.series.find((p) => p.t >= new Date(flow.t.getTime() - 86400000));
  const change = flow && dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;
  const stage = live?.stage;
  const action = meta.flood_stage_ft?.action;

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
          {live ? `Live from USGS · ${fmt.when(flow?.t ?? new Date())}` : error ? 'Live data unavailable' : 'Loading live data…'}
        </div>
      </div>

      <div className="card mt-6 grid grid-cols-2 overflow-hidden md:grid-cols-[1.4fr_1fr_1fr_1fr_1.6fr]">
        <Stat label="Flow now" big value={flow ? fmt.cfs(flow.v) : '—'} unit="cfs">
          {cls && <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${TONE[cls.tone]}`}>{cls.label}</span>}
        </Stat>
        <Stat label="Last 24 h" value={change == null ? '—' : `${change > 0 ? '↑' : change < 0 ? '↓' : '→'} ${Math.abs(Math.round(change * 100))}%`}>
          <span className="text-xs text-muted">{change == null ? '' : Math.abs(change) < 0.03 ? 'Steady' : change > 0 ? 'Rising' : 'Falling'}</span>
        </Stat>
        <Stat label="Normal today" value={normal ? fmt.cfs(normal.p50) : '—'} unit="cfs">
          <span className="text-xs text-muted">{normal ? `${fmt.cfs(normal.p25)}–${fmt.cfs(normal.p75)} typical` : ''}</span>
        </Stat>
        <Stat label="River level" value={stage ? stage.v.toFixed(1) : '—'} unit="ft">
          <span className="text-xs text-muted">{stage && action ? `${(action - stage.v).toFixed(1)} ft below action` : ''}</span>
          {live?.temp && <span className="text-xs text-muted">Water {fmt.f(live.temp.v)}</span>}
        </Stat>
        <div className="col-span-2 border-t border-line px-4 pt-3 pb-1 md:col-span-1 md:border-t-0 md:border-l">
          <div className="eyebrow">Past 7 days</div>
          {live?.series?.length ? (
            <PlotFigure deps={[live]} build={(w) => spark(live.series, clim, w)} />
          ) : (
            <div className="h-16" />
          )}
        </div>
      </div>
    </header>
  );
}

function Stat({ label, value, unit, big, children }) {
  return (
    <div className="border-line px-5 py-4 not-first:border-l max-md:[&:nth-child(3)]:border-l-0 max-md:[&:nth-child(n+3)]:border-t">
      <div className="eyebrow">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`${big ? 'text-3xl' : 'text-2xl'} font-semibold tracking-tight tabular-nums`}>{value}</span>
        {unit && <span className="text-sm text-muted">{unit}</span>}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">{children}</div>
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
