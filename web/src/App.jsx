import { useEffect, useMemo, useState } from 'react';
import Editorial from './components/Editorial.jsx';
import WarmingUp from './components/WarmingUp.jsx';
import SiteBar, { BAR_HEIGHT } from './components/SiteBar.jsx';
import { GaugeNotForecastable, GaugeUnsupported, SiteLoading, SiteNotFound, SitesError } from './components/SiteStatus.jsx';
import { isApiError, loadForecast, loadLive, loadStatic, loadStatus, postVisit, toSiteData } from './lib/site.ts';
import { loadCatalog, reasonText } from './lib/places.ts';
import { lastSite, navigate, rememberSite, sitePath, useRoute } from './lib/router.js';
import { usePlace, usePlaces } from './lib/usePlaces.js';
import { titleOf } from './lib/search.js';
import { fmt } from './lib/data.js';

// serving/schema/README.md (PR #64): poll every 10 s while waking, give up after 3 minutes, and keep drawing the
// previous forecast while waking if it is at most 3 days old.
const POLL_MS = 10000;
const POLL_FOR_MS = 3 * 60000;
const KEEP_WHILE_WAKING_MS = 3 * 86400e3;

const layerParam = new URLSearchParams(location.search).get('layer');
const NOT_SERVED = 'Not one of the basins flowcast forecasts yet';

export default function App() {
  const route = useRoute();
  const { places, defaultId, error } = usePlaces();
  const { place, status } = usePlace(route, places);

  // `/` opens the last site viewed here, else the index's default.
  useEffect(() => {
    if (route.site !== null) return;
    const start = lastSite() ?? defaultId;
    if (start) navigate(sitePath(start), { replace: true });
  }, [route, defaultId]);
  // Slugs (/site/callicoon) are aliases: show the canonical id in the address bar.
  useEffect(() => {
    if (!place) return;
    if (place.id !== route.site) navigate(sitePath(place.id), { replace: true });
    if (place.forecastable) rememberSite(place.id);
    document.title = `${titleOf(place)} · flowcast`;
  }, [place]);

  return (
    <>
      <SiteBar sites={places} current={place} onSelect={(id) => navigate(sitePath(id))} />
      <div className={BAR_HEIGHT} aria-hidden />
      {page()}
    </>
  );

  function page() {
    if (error) return <SitesError error={error} />;
    if (!places) return null;
    if (status === 'loading') return <SiteLoading site={null} />;
    if (status === 'unsupported') return <GaugeUnsupported id={route.site} />;
    if (!place) return <SiteNotFound id={route.slug ?? route.invalid} />;
    if (!place.forecastable) return <GaugeNotForecastable site={place} />;
    if (!place.summary) return <GaugeNotForecastable site={{ ...place, reason: NOT_SERVED }} />;
    return <Site key={place.id} place={place} />;
  }
}

/**
 * One index site: draws live.json and its current forecast, sends the visit that wakes it, and follows a wake run.
 * A site that has never been forecast has no live.json until its first run publishes one.
 */
function Site({ place }) {
  const summary = place.summary;
  const [live, setLive] = useState(undefined);
  const [stat, setStatic] = useState(null);
  const [forecast, setForecast] = useState(null);
  const [fetching, setFetching] = useState(null);
  // undefined until the visit answers; null when no wake could be started.
  const [wake, setWake] = useState(undefined);
  const [refused, setRefused] = useState(null);
  const [error, setError] = useState(null);
  const fail = (e) => setError(String(e.message ?? e));

  useEffect(() => {
    let stopped = false;
    let timer;
    let shown = null;
    let current = null;
    const show = (pointer, state) => {
      if (!pointer || pointer.issue === shown) return;
      if (state === 'waking' && Date.now() - Date.parse(pointer.issue_time) > KEEP_WHILE_WAKING_MS) return;
      shown = pointer.issue;
      setFetching(pointer.issue);
      loadForecast(pointer).then((f) => {
        if (stopped) return;
        if (f) setForecast(f);
        setFetching(null);
      }, fail);
    };
    const readLive = async () => {
      const l = await loadLive(summary.id);
      if (stopped) return null;
      setLive(l);
      if (l) loadStatic(l).then((st) => !stopped && setStatic(st), fail);
      current = l;
      return l;
    };
    // eta_s comes with the visit; status answers carry the run's start.
    const answer = (a) =>
      setWake((prev) => ({
        status: a.status,
        etaS: a.eta_s ?? prev?.etaS ?? null,
        startedAt: a.run?.started ? new Date(a.run.started) : (prev?.startedAt ?? new Date()),
        error: false,
      }));
    (async () => {
      const l = await readLive();
      if (stopped) return;
      const visit = await postVisit(summary, l);
      if (stopped) return;
      if (isApiError(visit)) {
        // 409 not_forecastable: the index said forecastable, but the rule has excluded the site since.
        const rule = await loadCatalog().then(
          (index) => index.rule,
          () => null,
        );
        if (!stopped) setRefused(reasonText([visit.detail ?? visit.error], rule));
        return;
      }
      const first = visit ?? (l && { status: l.status, forecast: l.forecast });
      if (first) answer(first);
      else setWake(null);
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
          answer(s);
          if (s.status !== 'waking') {
            readLive();
            return;
          }
        } else {
          setWake((w) => w && { ...w, error: true });
        }
        if (Date.now() > until) return setWake((w) => w && { ...w, status: 'paused' });
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
  const status = wake?.status ?? null;
  if (error) return <Message title="Couldn’t read this forecast">{error}</Message>;
  if (refused) return <GaugeNotForecastable site={{ ...place, reason: refused }} />;
  if (live === undefined || wake === undefined || (fetching && !forecast)) return <SiteLoading site={place} />;
  if (!site?.forecast) return <WarmingUp place={place} live={live} wake={wake} />;
  const issued = fmt.whenYear(new Date(site.forecast.issued));
  const banner =
    status === 'waking'
      ? 'Updating the forecast…'
      : status === 'delayed'
        ? `Forecast delayed. Last updated ${issued}.`
        : status === 'paused'
          ? `Showing the forecast from ${issued}; live updates are paused.`
          : null;
  return (
    <>
      {banner && <div className="bg-flow/[0.07] px-5 py-2 text-center text-[13px] text-flow">{banner}</div>}
      <Editorial site={site} updating={status === 'waking'} layer={layerParam} />
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
