import { useEffect, useState } from 'react';
import Editorial from './components/Editorial.jsx';
import WarmingUp from './components/WarmingUp.jsx';
import { loadSite, loadSiteIndex, loadStatus, postVisit } from './lib/site.ts';
import { sitePath, useRoute } from './lib/router.js';
import { fmt } from './lib/data.js';

/** How often to ask whether a warming forecast is ready. */
const POLL_MS = 15000;

const layerParam = new URLSearchParams(location.search).get('layer');

export default function App() {
  const route = useRoute();
  const [index, setIndex] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    loadSiteIndex().then(setIndex, (e) => setError(String(e.message ?? e)));
  }, []);

  if (error) return <Message title="Couldn’t load flowcast">{error}</Message>;
  if (!index) return <Message>Loading…</Message>;
  const id = route.site ?? index.default;
  const summary = index.sites.find((s) => s.id === id);
  if (route.invalid || !summary) {
    return (
      <Message title="No forecast here">
        flowcast doesn’t forecast {route.site ? `USGS ${route.site}` : 'this page'}.{' '}
        <a className="underline underline-offset-2" href={sitePath(index.default)}>
          See {index.sites.find((s) => s.id === index.default).name}
        </a>
        .
      </Message>
    );
  }
  return <Site key={id} summary={summary} />;
}

/** One site: posts the visit that wakes its forecasting, then shows its bundle and polls status while it warms up. */
function Site({ summary }) {
  const [bundle, setBundle] = useState(undefined);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    document.title = `${summary.river} at ${summary.place} · flowcast`;
    postVisit(summary.id);
  }, [summary]);

  useEffect(() => {
    let stale = false;
    loadSite(summary.id).then(
      (b) => !stale && setBundle(b),
      (e) => !stale && setError(String(e.message ?? e)),
    );
    return () => {
      stale = true;
    };
  }, [summary.id, version]);

  useEffect(() => {
    let timer;
    let stopped = false;
    let last = null;
    const poll = async () => {
      const s = await loadStatus(summary.id);
      if (stopped) return;
      setStatus(s);
      // A run finished since the bundle was read: read it again.
      if (s?.state === 'ready' && last && last !== 'ready') setVersion((v) => v + 1);
      last = s?.state ?? last;
      if (s && s.state !== 'ready') timer = setTimeout(poll, POLL_MS);
    };
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [summary.id]);

  const warming = status != null && status.state !== 'ready';
  if (error) return <Message title="Couldn’t read this forecast">{error}</Message>;
  if (bundle === undefined) return <Message>Loading {summary.name}…</Message>;
  if (bundle === null) {
    return (
      <div className="mx-auto max-w-5xl px-5 pt-10 pb-20 sm:px-8 sm:pt-14">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
          {summary.river} <span className="font-normal text-muted">at {summary.place}</span>
        </h1>
        <WarmingUp short={summary.short} className="mt-10" />
      </div>
    );
  }
  return (
    <>
      {warming && bundle.flow_forecast && (
        <div className="bg-flow/[0.07] px-5 py-2 text-center text-[13px] text-flow">
          Forecast warming up · showing the last run, from {fmt.whenYear(new Date(bundle.flow_forecast.issued_at * 1000))}
        </div>
      )}
      <Editorial bundle={bundle} warming={warming} layer={layerParam} />
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
