import { useEffect, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from '../components/PlotFigure.jsx';
import { Loader, Skeleton } from './Skeleton.jsx';
import { C } from '../lib/palette.js';
import { fmt, liveGauge, withGaps } from '../lib/data.js';
import { placeOf } from './sites.js';

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const MARGIN = { marginLeft: 44, marginRight: 16 };
// Same frame as the river page's flow view (rain strip and caption above the chart), so the forecast lands in place.
const FLOW_VIEW_EXTRA = 86;
const chartHeight = (width) => Math.max(300, Math.min(440, width * 0.42)) + FLOW_VIEW_EXTRA;

/**
 * A site whose forecast is still being made: the forecast page's layout, with live USGS readings where they exist,
 * shimmer where the forecast will go, and one loader in the middle of the chart.
 */
export default function ForecastWarming({ site, visit }) {
  const [gauge, setGauge] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    setGauge(null);
    setError(false);
    liveGauge(site.usgsId).then(setGauge, () => setError(true));
  }, [site.usgsId]);

  const flow = gauge?.flow;
  const dayAgo = flow && gauge.series.find((p) => p.t >= new Date(flow.t.getTime() - DAY));
  const d = flow && dayAgo ? (flow.v - dayAgo.v) / dayAgo.v : null;
  const trend = d == null ? null : Math.abs(d) < 0.03 ? 'Steady over 24 hours' : `${d > 0 ? '↑' : '↓'} ${d >= 1 ? `${(1 + d).toFixed(1)}×` : `${Math.abs(Math.round(d * 100))}%`} in 24 hours`;
  const trendLabel = d == null ? null : Math.abs(d) < 0.03 ? 'Steady' : d > 0 ? 'Rising' : 'Falling';
  const pending = !gauge && !error;

  return (
    <div className="editorial mx-auto max-w-5xl px-5 pb-20 sm:px-8">
      <header className="pt-10 sm:pt-14">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {site.river} {site.town && <span className="font-normal text-muted">at {placeOf(site)}</span>}
          </h1>
          <p className="flex items-baseline gap-1 text-[13px] text-muted">
            {flow ? `Updated ${fmt.whenYear(flow.t)} ·` : pending ? <Skeleton className="mr-1 h-3 w-44 self-center" /> : null} USGS {site.usgsId}
          </p>
        </div>

        <div className="mt-6 grid gap-x-12 gap-y-8 border-t border-line pt-6 md:grid-cols-[1fr_1fr_1.1fr]">
          <div>
            <div className="text-[13px] text-muted">Now</div>
            {pending ? (
              <SlotSkeleton />
            ) : (
              <>
                <div className="mt-2 flex items-center gap-2 text-[15px] font-semibold">
                  <span className="size-3 rounded-full" style={{ background: C.faint }} />
                  {error ? 'Live data unavailable' : (trendLabel ?? 'No recent reading')}
                </div>
                <div className="mt-1 flex items-baseline gap-2">
                  <span className="text-[2.6rem] leading-none font-semibold tracking-tight tabular-nums">{flow ? fmt.cfs(flow.v) : '—'}</span>
                  <span className="text-base text-muted">cfs</span>
                </div>
                <div className="mt-2 text-[13px] text-muted">{error ? '' : trend}</div>
              </>
            )}
          </div>

          <div>
            <div className="text-[13px] text-muted">Next 7 days</div>
            <SlotSkeleton />
          </div>

          <div>
            <div className="text-[13px] text-muted">River level</div>
            <div className="mt-1 flex h-8 items-baseline gap-3">
              {pending ? (
                <Skeleton className="h-7 w-24 self-center" />
              ) : (
                <span className="text-2xl font-semibold tracking-tight tabular-nums">{gauge?.stage ? `${gauge.stage.v.toFixed(1)} ft` : '—'}</span>
              )}
            </div>
            <Skeleton className="mt-2 h-2 max-w-64" />
            <dl className="mt-4 grid grid-cols-2 gap-x-6 text-[13px]">
              <div>
                <dt className="text-muted">Water</dt>
                <dd className="font-medium tabular-nums">
                  {!site.temperature ? <span className="font-normal text-muted">Not measured</span> : pending ? <Skeleton className="mt-1 h-3.5 w-12" /> : gauge?.temp ? fmt.f(gauge.temp.v) : '—'}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Drainage area</dt>
                <dd className="font-medium tabular-nums">{site.areaMi2 == null ? '—' : `${fmt.int(site.areaMi2)} mi²`}</dd>
              </div>
            </dl>
          </div>
        </div>
      </header>

      <section className="mt-12">
        <h2 className="text-lg font-semibold">Flow{site.town ? ` at ${site.town}` : ''}</h2>
        <p className="mt-1 text-sm text-muted">
          The past three days and the seven-day forecast.
          {site.temperature ? '' : ' This gauge doesn’t measure water temperature, so the forecast is flow only.'}
        </p>
        <div className="relative mt-5">
          <PlotFigure deps={[gauge]} build={(w) => pastChart(gauge?.series ?? [], w)} />
          <div
            className="absolute top-0 overflow-hidden rounded-r-lg"
            style={{ left: `calc(${MARGIN.marginLeft}px + (100% - ${MARGIN.marginLeft + MARGIN.marginRight}px) * 0.3)`, right: MARGIN.marginRight, bottom: 30 }}
          >
            <Skeleton className="absolute inset-0 rounded-none opacity-60" />
            <div className="absolute inset-0 grid place-items-center">
              <WarmupLoader visit={visit} />
            </div>
          </div>
        </div>
        <div className="mt-3 border-t border-line pt-3 text-[13px] text-muted">
          <a className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://waterdata.usgs.gov/monitoring-location/${site.id}/`} target="_blank" rel="noreferrer">
            USGS gauge page
          </a>
        </div>
      </section>
    </div>
  );
}

function SlotSkeleton() {
  return (
    <div aria-hidden>
      <Skeleton className="mt-2.5 h-4 w-32" />
      <Skeleton className="mt-2.5 h-9 w-40" />
      <Skeleton className="mt-3 h-3 w-28" />
    </div>
  );
}

/** The page's single loading message: how far the first run has got, or that it stalled. */
function WarmupLoader({ visit }) {
  const [now, setNow] = useState(() => Date.now());
  const [answeredAt, setAnsweredAt] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  // eta_s counts from when the visit or status call answered, not from when the run started.
  useEffect(() => setAnsweredAt(Date.now()), [visit?.etaS]);
  const elapsed = visit ? (now - visit.startedAt.getTime()) / 1000 : 0;
  const left = visit?.etaS == null ? null : Math.max(0, visit.etaS - (now - answeredAt) / 1000);
  const progress = left == null ? null : Math.min(0.95, elapsed / (elapsed + left || 1));

  if (visit?.paused) return <Loader stalled label="Taking longer than expected" detail="Reload in a few minutes to check again" />;
  const detail = visit?.error
    ? 'Reconnecting…'
    : left == null
      ? 'First run for this river'
      : left === 0
        ? 'Almost there'
        : `About ${left > 90 ? `${Math.ceil(left / 60)} min` : `${Math.max(5, Math.ceil(left / 5) * 5)} s`}`;
  return <Loader progress={progress} label="Warming up the forecast" detail={detail} />;
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
    y: vals.length ? { domain: [0, Math.max(...vals) * 1.6], ticks: 5, tickSize: 0, tickFormat: kcfs, grid: true, label: null } : { axis: null, domain: [0, 1] },
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
