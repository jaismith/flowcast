import { useEffect, useMemo, useState } from 'react';
import Editorial, { placeName } from './components/Editorial.jsx';
import WarmingUp from './components/WarmingUp.jsx';
import { isApiError, loadForecast, loadLive, loadSiteIndex, loadStatic, loadStatus, postVisit, toSiteData } from './lib/site.ts';
import { sitePath, useRoute } from './lib/router.js';
import { fmt } from './lib/data.js';

// serving/schema/README.md (PR #64): poll every 10 s while waking, give up after 3 minutes, and keep drawing the
// previous forecast while waking if it is at most 3 days old.
const POLL_MS = 10000;
const POLL_FOR_MS = 3 * 60000;
const KEEP_WHILE_WAKING_MS = 3 * 86400e3;

const layerParam = new URLSearchParams(location.search).get('layer');

export default function App() {
  const route = useRoute();
  const [index, setIndex] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    loadSiteIndex().then(setIndex, (e) => setError(String(e.message ?? e)));
  }, []);

  const summary = useMemo(() => {
    if (!index) return null;
    if (route.slug) return index.sites.find((s) => s.slug === route.slug) ?? null;
    return index.sites.find((s) => s.id === (route.site ?? index.default)) ?? null;
  }, [index, route]);
  // Slugs (/site/callicoon) are aliases: show the canonical id in the address bar.
  useEffect(() => {
    if (route.slug && summary) history.replaceState(null, '', sitePath(summary.id) + location.search);
  }, [route.slug, summary]);

  if (error) return <Message title="Couldn’t load flowcast">{error}</Message>;
  if (!index) return <Message>Loading…</Message>;
  const fallback = index.sites.find((s) => s.id === index.default);
  if (route.invalid || !summary) {
    return (
      <Message title="No forecast here">
        flowcast doesn’t cover {route.site ?? route.slug ?? 'this page'}.{' '}
        <a className="underline underline-offset-2" href={sitePath(fallback.id)}>
          See {fallback.name}
        </a>
        .
      </Message>
    );
  }
  return <Site key={summary.id} summary={summary} />;
}

/**
 * One site: draws live.json and its current forecast, sends the visit that wakes it, and follows a wake run. A
 * site that has never been forecast has no live.json until its first run publishes one.
 */
function Site({ summary }) {
  const [live, setLive] = useState(undefined);
  const [stat, setStatic] = useState(null);
  const [forecast, setForecast] = useState(null);
  const [status, setStatus] = useState(null);
  const [refused, setRefused] = useState(null);
  const [error, setError] = useState(null);
  const fail = (e) => setError(String(e.message ?? e));

  useEffect(() => {
    document.title = `${summary.river} at ${summary.town}, ${summary.state} · flowcast`;
    let stopped = false;
    let timer;
    let shown = null;
    let current = null;
    const show = (pointer, state) => {
      if (!pointer || pointer.issue === shown) return;
      if (state === 'waking' && Date.now() - Date.parse(pointer.issue_time) > KEEP_WHILE_WAKING_MS) return;
      shown = pointer.issue;
      loadForecast(pointer).then((f) => !stopped && setForecast(f), fail);
    };
    const readLive = async () => {
      const l = await loadLive(summary.id);
      if (stopped) return null;
      setLive(l);
      if (l) loadStatic(l).then((st) => !stopped && setStatic(st), fail);
      current = l;
      return l;
    };
    (async () => {
      const l = await readLive();
      if (stopped) return;
      const visit = await postVisit(summary, l);
      if (stopped) return;
      if (isApiError(visit)) return setRefused(visit.detail ?? visit.error);
      const first = visit ?? (l && { status: l.status, forecast: l.forecast });
      setStatus(first?.status ?? null);
      if (l) show(first.forecast ?? l.forecast, first.status);
      if (first?.status !== 'waking') return;
      const until = Date.now() + POLL_FOR_MS;
      const poll = async () => {
        const s = await loadStatus(summary.id);
        if (stopped) return;
        if (s && !isApiError(s)) {
          // A site's first forecast also publishes its live.json.
          if (s.forecast && !current) await readLive();
          if (stopped) return;
          if (current) show(s.forecast, s.status);
          setStatus(s.status);
          if (s.status !== 'waking') {
            readLive();
            return;
          }
        }
        if (Date.now() > until) return setStatus('paused');
        timer = setTimeout(poll, POLL_MS);
      };
      timer = setTimeout(poll, POLL_MS);
    })().catch(fail);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [summary]);

  const site = useMemo(() => live && toSiteData(summary, live, stat, forecast), [summary, live, stat, forecast]);
  if (error) return <Message title="Couldn’t read this forecast">{error}</Message>;
  if (refused) return <Message title={summary.name}>flowcast can’t forecast this gauge yet ({refused}).</Message>;
  if (live === undefined) return <Message>Loading {summary.name}…</Message>;
  if (!site || (!site.forecast && status === 'waking')) {
    return (
      <div className="mx-auto max-w-5xl px-5 pt-10 pb-20 sm:px-8 sm:pt-14">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
          {summary.river} <span className="font-normal text-muted">at {placeName(`${summary.town}, ${summary.state}`)}</span>
        </h1>
        {status === 'waking' || status === 'paused' ? (
          <WarmingUp short={summary.town} paused={status === 'paused'} className="mt-10" />
        ) : (
          <p className="mt-6 text-sm text-muted">flowcast hasn’t forecast this river yet, and couldn’t start a forecast just now. Try again in a few minutes.</p>
        )}
      </div>
    );
  }
  const issued = site.forecast && fmt.whenYear(new Date(site.forecast.issued));
  const banner =
    status === 'waking'
      ? 'Updating the forecast…'
      : status === 'delayed' && issued
        ? `Forecast delayed. Last updated ${issued}.`
        : status === 'paused' && issued
          ? `Showing the forecast from ${issued}; live updates are paused.`
          : null;
  return (
    <>
      {banner && site.forecast && <div className="bg-flow/[0.07] px-5 py-2 text-center text-[13px] text-flow">{banner}</div>}
      <Editorial site={site} updating={status === 'waking'} paused={status === 'paused'} layer={layerParam} />
    </>
  );
}

function Message({ title, children }) {
  return (
    <div className="mx-auto max-w-5xl px-5 pt-14 sm:px-8">
      {title && <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>}
      <p className="mt-2 text-sm text-muted">{children}</p>
    </div>
  );
}
