import { useEffect, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from '../components/PlotFigure.jsx';
import { C } from '../lib/palette.js';
import { fmt, liveGauge, withGaps } from '../lib/data.js';
import { usgsNumber } from './sites.js';

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const MARGIN = { marginLeft: 44, marginRight: 16 };
// Same frame as the river page's flow view (rain strip and caption above the chart), so the forecast lands in place.
const FLOW_VIEW_EXTRA = 86;
const chartHeight = (width) => Math.max(300, Math.min(440, width * 0.42)) + FLOW_VIEW_EXTRA;

/**
 * A site whose forecast is still being made. Live gauge readings fill the slots the forecast page uses, so when the
 * forecast arrives the page fills in rather than rearranging.
 */
export default function ForecastWarming({ site, warmup }) {
  const num = usgsNumber(site.id);
  const [gauge, setGauge] = useState(null);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setGauge(null);
    setError(false);
    liveGauge(num).then(setGauge, () => setError(true));
  }, [num]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const flow = gauge?.flow;
  const dayAgo = flow && gauge.series.find((p) => p.t >= new Date(flow.t.getTime() - DAY));
  const d = flow && dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;
  const trend = d == null ? '' : Math.abs(d) < 0.03 ? 'Steady over 24 hours' : `${d > 0 ? '↑' : '↓'} ${d >= 1 ? `${(1 + d).toFixed(1)}×` : `${Math.abs(Math.round(d * 100))}%`} in 24 hours`;
  const trendLabel = d == null ? null : Math.abs(d) < 0.03 ? 'Steady' : d > 0 ? 'Rising' : 'Falling';

  const elapsed = warmup ? (now - warmup.startedAt.getTime()) / 1000 : 0;
  const eta = warmup?.etaS;
  const progress = eta ? Math.min(0.94, elapsed / eta) : null;
  const late = eta != null && elapsed > eta;
  const left = eta == null ? null : Math.max(0, eta - elapsed);

  return (
    <div className="editorial mx-auto max-w-5xl px-5 pb-20 sm:px-8">
      <header className="pt-10 sm:pt-14">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {site.river} <span className="font-normal text-muted">at {site.town}, {site.state}</span>
          </h1>
          <p className="text-[13px] text-muted">
            {flow ? `Updated ${fmt.whenYear(flow.t)}` : error ? 'Live data unavailable' : 'Loading live data…'} · USGS {num}
          </p>
        </div>

        <div className="mt-6 grid gap-x-12 gap-y-8 border-t border-line pt-6 md:grid-cols-[1fr_1fr_1.1fr]">
          <div>
            <div className="text-[13px] text-muted">Now</div>
            <div className="mt-2 flex items-center gap-2 text-[15px] font-semibold">
              <span className="size-3 rounded-full" style={{ background: C.faint }} />
              {trendLabel ?? '…'}
            </div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="text-[2.6rem] leading-none font-semibold tracking-tight tabular-nums">{flow ? fmt.cfs(flow.v) : '—'}</span>
              <span className="text-base text-muted">cfs</span>
            </div>
            <div className="mt-2 text-[13px] text-muted">{trend}</div>
          </div>

          <div aria-live="polite">
            <div className="text-[13px] text-muted">Next 7 days</div>
            <div className="mt-2 flex items-center gap-2 text-[15px] font-semibold">
              <span className="relative flex size-3">
                <span className="absolute inset-0 animate-ping rounded-full opacity-40" style={{ background: C.flow }} />
                <span className="relative size-3 rounded-full" style={{ background: C.flow }} />
              </span>
              Forecast warming up
            </div>
            <div className="mt-1 flex h-[2.6rem] items-center">
              <div className="h-1.5 w-full max-w-64 overflow-hidden rounded-full bg-normal">
                <div
                  className={`h-full rounded-full transition-[width] duration-1000 ease-linear ${progress == null || late ? 'animate-pulse' : ''}`}
                  style={{ width: `${Math.round((late ? 1 : (progress ?? 0.08)) * 100)}%`, background: C.flow }}
                />
              </div>
            </div>
            <div className="mt-2 text-[13px] text-muted">
              {warmup?.error
                ? 'Couldn’t reach the forecaster. Retrying…'
                : late
                  ? 'Taking longer than usual. This page updates by itself.'
                  : left != null
                    ? `First run for this river · about ${left > 90 ? `${Math.ceil(left / 60)} min` : `${Math.max(5, Math.ceil(left / 5) * 5)} s`} left`
                    : 'Starting the first run for this river…'}
            </div>
          </div>

          <div>
            <div className="text-[13px] text-muted">River level</div>
            <div className="mt-1 flex items-baseline gap-3">
              <span className="text-2xl font-semibold tracking-tight tabular-nums">{gauge?.stage ? `${gauge.stage.v.toFixed(1)} ft` : '—'}</span>
              <span className="text-[13px] text-muted">{gauge && !gauge.stage ? 'No stage reported' : ''}</span>
            </div>
            <div className="mt-2 h-2 max-w-64 rounded-full bg-normal" />
            <dl className="mt-4 grid grid-cols-2 gap-x-6 text-[13px]">
              <div>
                <dt className="text-muted">Water</dt>
                <dd className="font-medium tabular-nums">{gauge?.temp ? fmt.f(gauge.temp.v) : '—'}</dd>
              </div>
              <div>
                <dt className="text-muted">Drainage area</dt>
                <dd className="font-medium tabular-nums">{fmt.int(site.area_mi2)} mi²</dd>
              </div>
            </dl>
          </div>
        </div>
      </header>

      <section className="mt-12">
        <h2 className="text-lg font-semibold">Flow at {site.town}</h2>
        <p className="mt-1 text-sm text-muted">The past three days, live from USGS. The seven-day forecast appears here as soon as it’s ready.</p>
        <div className="relative mt-5">
          <PlotFigure deps={[gauge]} build={(w) => pastChart(gauge?.series ?? [], w)} />
          <div
            className="pointer-events-none absolute top-0 grid place-items-center"
            style={{ left: `calc(${MARGIN.marginLeft}px + (100% - ${MARGIN.marginLeft + MARGIN.marginRight}px) * 0.3)`, right: MARGIN.marginRight, bottom: 30 }}
          >
            <div className="absolute inset-0 animate-pulse rounded-r-lg" style={{ background: `${C.flow}0d` }} />
            <div className="relative text-center">
              <div className="text-sm font-semibold" style={{ color: C.flow }}>
                Forecast warming up
              </div>
              <div className="mt-0.5 text-[12.5px] text-muted">flowcast makes a river’s forecast the first time it’s opened</div>
            </div>
          </div>
        </div>
        <div className="mt-3 border-t border-line pt-3 text-[13px] text-muted">
          {warmup ? `Forecast run started ${fmt.when(warmup.startedAt)}` : 'Starting a forecast run'} ·{' '}
          <a className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://waterdata.usgs.gov/monitoring-location/${site.id}/`} target="_blank" rel="noreferrer">
            USGS gauge page
          </a>
        </div>
      </section>
    </div>
  );
}

function pastChart(series, width) {
  const now = series.at(-1)?.t ?? new Date();
  const from = new Date(now.getTime() - 3 * DAY);
  const to = new Date(now.getTime() + 7 * DAY);
  const past = withGaps(series.filter((p) => p.t >= from));
  const vals = past.map((p) => p.v).filter((v) => v != null);
  const last = past.findLast((p) => p.v != null);
  const halo = { stroke: C.paper, strokeWidth: 4, paintOrder: 'stroke' };
  return Plot.plot({
    width,
    height: chartHeight(width),
    ...MARGIN,
    marginTop: 8,
    marginBottom: 30,
    x: {
      domain: [from, to],
      ticks: 'day',
      tickFormat: (d) => d.toLocaleDateString('en-US', { weekday: 'short', timeZone: 'America/New_York' }),
      tickSize: 0,
      label: null,
    },
    y: { domain: [0, vals.length ? Math.max(...vals) * 1.6 : 1], ticks: 5, tickSize: 0, tickFormat: kcfs, grid: true, label: null },
    style: { fontFamily: 'inherit', fontSize: '12px', color: C.muted, overflow: 'visible' },
    marks: [
      Plot.ruleX([now], { stroke: C.ink, strokeOpacity: 0.35, strokeDasharray: '2,3' }),
      Plot.lineY(past, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 2 }),
      last ? Plot.dot([last], { x: 't', y: 'v', r: 4.5, fill: C.ink, stroke: C.paper, strokeWidth: 2 }) : null,
      last ? Plot.text([last], { x: 't', y: 'v', text: (p) => `Now ${fmt.cfs(p.v)}`, textAnchor: 'end', dx: -8, dy: -12, fill: C.ink, fontWeight: 600, ...halo }) : null,
    ].filter(Boolean),
  });
}

const kcfs = (v) => (v >= 10000 ? `${Math.round(v / 1000)}k` : v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : `${Math.round(v)}`);
