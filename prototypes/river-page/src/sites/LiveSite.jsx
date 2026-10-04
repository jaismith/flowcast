import { useEffect, useState } from 'react';
import * as Plot from '@observablehq/plot';
import PlotFigure from '../components/PlotFigure.jsx';
import { Loader, Skeleton } from './Skeleton.jsx';
import { C } from '../lib/palette.js';
import { fmt, withGaps } from '../lib/data.js';
import { placeOf } from './sites.js';

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const MARGIN = { marginLeft: 44, marginRight: 16 };
// Same frame as the river page's flow view (rain strip and caption above the chart).
const FLOW_VIEW_EXTRA = 86;
const chartHeight = (width) => Math.max(300, Math.min(440, width * 0.42)) + FLOW_VIEW_EXTRA;
const PAST_SHARE = 0.3;

/**
 * A site served by the live backend: readings from live.json, and the newest forecast once one exists. Until then
 * everything still to come is shimmer, with one loader in the middle of the forecast area.
 */
export default function LiveSite({ site, visit, live, liveDone, forecast }) {
  const now = live?.now;
  const pending = !now;
  const flow = now?.flow_cfs;
  const change = now?.flow_change_24h_cfs;
  const d = flow != null && change != null && flow - change > 0 ? change / (flow - change) : null;
  const trend = d == null ? null : Math.abs(d) < 0.03 ? 'Steady over 24 hours' : `${d > 0 ? '↑' : '↓'} ${d >= 1 ? `${(1 + d).toFixed(1)}×` : `${Math.abs(Math.round(d * 100))}%`} in 24 hours`;
  const trendLabel = d == null ? 'Latest reading' : Math.abs(d) < 0.03 ? 'Steady' : d > 0 ? 'Rising' : 'Falling';
  const outlook = forecast && outlookOf(forecast);
  const status = visit?.status ?? live?.status;

  return (
    <div className="editorial mx-auto max-w-5xl px-5 pb-20 sm:px-8">
      <header className="pt-10 sm:pt-14">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {site.river} {site.town && <span className="font-normal text-muted">at {placeOf(site)}</span>}
          </h1>
          <p className="flex items-baseline gap-1 text-[13px] text-muted">
            {now?.observed_at ? `Updated ${fmt.whenYear(new Date(now.observed_at))} ·` : !liveDone ? <Skeleton className="mr-1 h-3 w-44 self-center" /> : null} USGS {site.usgsId}
          </p>
        </div>

        <div className="mt-6 grid gap-x-12 gap-y-8 border-t border-line pt-6 md:grid-cols-[1fr_1fr_1.1fr]">
          <div>
            <div className="text-[13px] text-muted">Now</div>
            {pending ? (
              <SlotSkeleton />
            ) : (
              <Slot label={trendLabel} color={C.faint} value={flow == null ? '—' : fmt.cfs(flow)} detail={now.gauge_stale ? 'Gauge hasn’t reported for over 3 hours' : trend} />
            )}
          </div>

          <div>
            <div className="text-[13px] text-muted">Next 7 days</div>
            {outlook ? <Slot label={outlook.label} color={C.flow} caption={outlook.caption} value={fmt.cfs(outlook.value)} detail={outlook.detail} /> : <SlotSkeleton />}
          </div>

          <div>
            <div className="text-[13px] text-muted">River level</div>
            <div className="mt-1 flex h-8 items-baseline gap-3">
              {pending ? (
                <Skeleton className="h-7 w-24 self-center" />
              ) : (
                <>
                  <span className="text-2xl font-semibold tracking-tight tabular-nums">{now.stage_ft == null ? '—' : `${now.stage_ft.toFixed(1)} ft`}</span>
                  <span className="text-[13px] text-muted">{FLOOD_LABEL[now.flood_category] ?? ''}</span>
                </>
              )}
            </div>
            {pending ? <Skeleton className="mt-2 h-2 max-w-64" /> : <div className="mt-2 h-2" />}
            <dl className="mt-4 grid grid-cols-2 gap-x-6 text-[13px]">
              <div>
                <dt className="text-muted">Water</dt>
                <dd className="font-medium tabular-nums">
                  {pending ? (
                    site.temperature ? (
                      <Skeleton className="mt-1 h-3.5 w-12" />
                    ) : (
                      <span className="font-normal text-muted">Not forecast</span>
                    )
                  ) : now.water_temp_c != null ? (
                    fmt.f(now.water_temp_c)
                  ) : (
                    <span className="font-normal text-muted">Not measured</span>
                  )}
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
          {site.temperature ? '' : ' flowcast forecasts flow only here; water temperature isn’t forecast for this gauge.'}
        </p>
        <div className="relative mt-5">
          <PlotFigure deps={[live, forecast]} build={(w) => flowChart(live, forecast, w)} />
          {!forecast && (
            <div
              className="absolute top-0 overflow-hidden rounded-r-lg"
              style={{ left: `calc(${MARGIN.marginLeft}px + (100% - ${MARGIN.marginLeft + MARGIN.marginRight}px) * ${PAST_SHARE})`, right: MARGIN.marginRight, bottom: 30 }}
            >
              <Skeleton className="absolute inset-0 rounded-none opacity-60" />
              <div className="absolute inset-0 grid place-items-center">
                <WarmupLoader visit={visit} />
              </div>
            </div>
          )}
        </div>
        <div className="mt-3 flex flex-wrap justify-between gap-x-6 gap-y-1 border-t border-line pt-3 text-[13px] text-muted">
          <span>{forecast ? [`Forecast issued ${fmt.whenYear(new Date(forecast.issue_time))}`, STATUS_NOTE[status]].filter(Boolean).join(' · ') : ''}</span>
          <a className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://waterdata.usgs.gov/monitoring-location/${site.id}/`} target="_blank" rel="noreferrer">
            USGS gauge page
          </a>
        </div>
      </section>
    </div>
  );
}

const FLOOD_LABEL = { none: 'Below flood stage', action: 'Action stage', minor: 'Minor flooding', moderate: 'Moderate flooding', major: 'Major flooding' };
const STATUS_NOTE = { waking: 'Updating the forecast…', delayed: 'Forecast delayed', paused: 'Live updates are paused' };

function Slot({ label, color, caption, value, detail }) {
  return (
    <>
      <div className="mt-2 flex items-center gap-2 text-[15px] font-semibold">
        <span className="size-3 rounded-full" style={{ background: color }} />
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        {caption && <span className="text-[13px] whitespace-nowrap text-muted">{caption}</span>}
        <span className="text-[2.6rem] leading-none font-semibold tracking-tight tabular-nums">{value}</span>
        <span className="text-base text-muted">cfs</span>
      </div>
      <div className="mt-2 text-[13px] text-muted">{detail}</div>
    </>
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

/** The forecast's headline for the week, from its `story`: the crest if the river is heading somewhere, else where it ends. */
function outlookOf(fc) {
  const { crest, trend } = fc.story ?? {};
  const label = trend ? trend[0].toUpperCase() + trend.slice(1) : 'Forecast';
  const end = fc.flow.q50.length - 1;
  if (crest && Date.parse(crest.time) - Date.parse(fc.issue_time) > 6 * HOUR && trend === 'rising') {
    return { label, caption: 'Peak', value: crest.q50, detail: `${fmt.day(new Date(crest.time))} · likely ${fmt.cfs(crest.q25)}–${fmt.cfs(crest.q75)} cfs` };
  }
  return { label, caption: 'In 7 days', value: fc.flow.q50[end], detail: `Likely ${fmt.cfs(fc.flow.q25[end])}–${fmt.cfs(fc.flow.q75[end])} cfs` };
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

/** Regular series {start, step_h, values} → [{t, v}] from `from` on. */
function points(series, from = -Infinity) {
  if (!series) return [];
  const t0 = Date.parse(series.start);
  const out = [];
  series.values.forEach((v, i) => {
    const t = t0 + i * series.step_h * HOUR;
    if (t >= from) out.push({ t: new Date(t), v });
  });
  return out;
}

function flowChart(live, fc, width) {
  const obs = live?.observations?.discharge;
  const lastObs = live?.now?.observed_at ? Date.parse(live.now.observed_at) : null;
  const issue = fc ? Date.parse(fc.issue_time) : null;
  const anchor = issue ?? lastObs ?? Date.now();
  const from = new Date(anchor - 3 * DAY);
  const to = new Date(anchor + 7 * DAY);
  const past = withGaps(points(obs, from.getTime()).filter((p) => p.v != null));
  const fan = fc
    ? points({ ...fc.flow, values: fc.flow.q50 }).map((p, i) => ({ t: p.t, q05: fc.flow.q05[i], q25: fc.flow.q25[i], q50: fc.flow.q50[i], q75: fc.flow.q75[i], q95: fc.flow.q95[i] }))
    : [];
  const vals = [...past.map((p) => p.v), ...fan.flatMap((r) => [r.q95, r.q05])].filter((v) => v != null);
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
    y: vals.length ? { domain: [0, Math.max(...vals) * (fc ? 1.15 : 1.6)], ticks: 5, tickSize: 0, tickFormat: kcfs, grid: true, label: null } : { axis: null, domain: [0, 1] },
    style: { fontFamily: 'inherit', fontSize: '12px', color: C.muted, overflow: 'visible' },
    marks: [
      fan.length ? Plot.areaY(fan, { x: 't', y1: 'q05', y2: 'q95', fill: C.flow, fillOpacity: 0.1, curve: 'monotone-x' }) : null,
      fan.length ? Plot.areaY(fan, { x: 't', y1: 'q25', y2: 'q75', fill: C.flow, fillOpacity: 0.2, curve: 'monotone-x' }) : null,
      Plot.ruleX([new Date(anchor)], { stroke: C.ink, strokeOpacity: 0.35, strokeDasharray: '2,3' }),
      fan.length ? Plot.lineY(fan, { x: 't', y: 'q50', stroke: C.flow, strokeWidth: 2.5, curve: 'monotone-x' }) : null,
      Plot.lineY(past, { x: 't', y: 'v', stroke: C.ink, strokeWidth: 2 }),
      fan.length ? Plot.text([fan[Math.round(fan.length * 0.7)]], { x: 't', y: 'q50', text: () => 'Forecast', dy: -10, fill: C.flow, fontWeight: 600, ...halo }) : null,
      last ? Plot.dot([last], { x: 't', y: 'v', r: 4.5, fill: C.ink, stroke: C.paper, strokeWidth: 2 }) : null,
      last ? Plot.text([last], { x: 't', y: 'v', text: (p) => `Now ${fmt.cfs(p.v)}`, textAnchor: 'end', dx: -8, dy: -12, fill: C.ink, fontWeight: 600, ...halo }) : null,
    ].filter(Boolean),
  });
}

const kcfs = (v) => (v >= 10000 ? `${Math.round(v / 1000)}k` : v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : `${Math.round(v)}`);
