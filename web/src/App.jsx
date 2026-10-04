import { useEffect, useMemo, useState } from 'react';
import Editorial from './components/Editorial.jsx';
import WarmingUp from './components/WarmingUp.jsx';
import { loadForecast, loadLive, loadSiteIndex, loadStatic, loadStatus, postVisit, toSiteData } from './lib/site.ts';
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
  if (!summary.forecast_ready) {
    return (
      <Message title={summary.name}>
        flowcast’s model covers this river, but live forecasts aren’t switched on for it yet.{' '}
        <a className="underline underline-offset-2" href={sitePath(fallback.id)}>
          See {fallback.name}
        </a>
        .
      </Message>
    );
  }
  return <Site key={summary.id} summary={summary} />;
}

/** One site: draws live.json and the current forecast, sends the visit that wakes it, and follows a wake run. */
function Site({ summary }) {
  const [live, setLive] = useState(undefined);
  const [stat, setStatic] = useState(null);
  const [forecast, setForecast] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const fail = (e) => setError(String(e.message ?? e));

  useEffect(() => {
    document.title = `${summary.river} at ${summary.town}, ${summary.state} · flowcast`;
    let stopped = false;
    let timer;
    let shown = null;
    const show = (pointer, state) => {
      if (!pointer || pointer.issue === shown) return;
      const old = Date.now() - Date.parse(pointer.issue_time) > KEEP_WHILE_WAKING_MS;
      if (state === 'waking' && old) return;
      shown = pointer.issue;
      loadForecast(pointer).then((f) => !stopped && setForecast(f), fail);
    };
    (async () => {
      const l = await loadLive(summary.id);
      if (stopped) return;
      setLive(l);
      if (!l) return;
      loadStatic(l).then((s) => !stopped && setStatic(s), fail);
      const visit = await postVisit(l);
      if (stopped) return;
      const first = visit ?? { status: l.status, forecast: l.forecast };
      setStatus(first.status);
      show(first.forecast ?? l.forecast, first.status);
      if (first.status !== 'waking') return;
      const until = Date.now() + POLL_FOR_MS;
      const poll = async () => {
        const s = await loadStatus(summary.id);
        if (stopped) return;
        if (s) {
          show(s.forecast, s.status);
          setStatus(s.status);
          if (s.status !== 'waking') return;
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
  if (live === undefined) return <Message>Loading {summary.name}…</Message>;
  if (live === null) return <Message title={summary.name}>This site has no live data yet.</Message>;
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
      {site.forecast || status !== 'waking' ? (
        <Editorial site={site} updating={status === 'waking'} paused={status === 'paused'} layer={layerParam} />
      ) : (
        <div className="mx-auto max-w-5xl px-5 pt-10 pb-20 sm:px-8 sm:pt-14">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {site.river} <span className="font-normal text-muted">at {site.place}</span>
          </h1>
          <WarmingUp short={site.short} className="mt-10" />
        </div>
      )}
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
