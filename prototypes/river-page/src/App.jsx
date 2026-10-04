import { useEffect, useState } from 'react';
import Header from './components/Header.jsx';
import Forecast from './components/Forecast.jsx';
import SiteMeta from './components/SiteMeta.jsx';
import Basin from './components/Basin.jsx';
import TimeTravel from './components/TimeTravel.jsx';
import Editorial from './components/Editorial.jsx';
import SiteBar, { BAR_HEIGHT } from './sites/SiteBar.jsx';
import LiveSite from './sites/LiveSite.jsx';
import { GaugeNotForecastable, GaugeUnsupported, SiteLoading, SiteNotFound, SitesError } from './sites/SiteStatus.jsx';
import { rememberSite, titleOf } from './sites/sites.js';
import { useForecast, useLive, useSite, useSiteRoute, useSites, useVisit } from './sites/useSites.js';
import { loadSite } from './lib/data.js';
import { applyTheme } from './lib/palette.js';

const params = new URLSearchParams(location.search);
// Sites with validation-year replay files (prototypes/landing/public/data/sites/), drawn by the editorial page.
// Every other site, and these with ?live, use the live backend.
const REPLAYS = new Set(['01427510', '01011000', '01654000']);

function initialClock() {
  const at = params.get('at') ? new Date(params.get('at')) : null;
  return { at: at && !isNaN(at) ? at : null, scenario: params.get('scenario'), layer: params.get('layer') };
}

export default function App() {
  const [siteId, goToSite] = useSiteRoute();
  const { sites, defaultId, error: sitesError } = useSites();
  const { site, status } = useSite(siteId, sites);
  const [landed, setLanded] = useState(null);
  // A wake run that lands publishes live.json too, so it's refetched when the visit reports a new forecast.
  const { live, done: liveDone } = useLive(site, landed);
  const visit = useVisit(site, live, liveDone);
  useEffect(() => setLanded(visit?.forecast?.issue ?? null), [visit?.forecast?.issue]);
  const pointer = visit?.forecast ?? live?.forecast ?? null;
  const { forecast } = useForecast(pointer?.url);
  const replay = !!site && REPLAYS.has(site.usgsId) && !params.has('live');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [clock, setClock] = useState(initialClock);
  const [theme, setThemeName] = useState(() => applyTheme(params.get('theme')));
  const [layout, setLayout] = useState(params.get('layout') === 'cards' ? 'cards' : 'editorial');
  const [issue, setIssue] = useState(null);
  const debug = import.meta.env.DEV;
  const setTheme = (name) => setThemeName(applyTheme(name));
  useEffect(() => {
    if (!siteId && defaultId) goToSite(defaultId, { replace: true });
  }, [siteId, defaultId]);
  useEffect(() => {
    if (!site) return;
    if (site.id !== siteId) goToSite(site.id, { replace: true });
    if (site.forecastable) rememberSite(site.id);
    document.title = `${titleOf(site)} · flowcast`;
  }, [site]);
  useEffect(() => {
    setData(null);
    setError(null);
    if (!replay) return;
    let stale = false;
    loadSite(site.usgsId).then(
      (d) => !stale && setData(d),
      (e) => !stale && setError(String(e)),
    );
    return () => {
      stale = true;
    };
  }, [replay, site?.id]);
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    for (const [k, v] of [
      ['at', clock.at?.toISOString()],
      ['scenario', clock.scenario],
      ['layer', clock.layer],
      ['theme', theme === 'modern' ? null : theme],
      ['layout', layout === 'editorial' ? null : layout],
    ]) {
      if (v) p.set(k, v);
      else p.delete(k);
    }
    history.replaceState(null, '', `${location.pathname}${p.size ? `?${p}` : ''}`);
  }, [clock, theme, layout]);

  return (
    <>
      <SiteBar key={theme} sites={sites} current={site} onSelect={goToSite} />
      <div className={BAR_HEIGHT} aria-hidden />
      {page()}
    </>
  );

  function page() {
    if (sitesError) return <SitesError error={sitesError} />;
    if (!sites) return null;
    if (status === 'loading') return <SiteLoading site={null} />;
    if (status === 'unsupported') return <GaugeUnsupported id={siteId} />;
    if (!site) return <SiteNotFound id={siteId} />;
    if (!site.forecastable) return <GaugeNotForecastable site={site} />;
    if (visit?.status === 'not_forecastable') return <GaugeNotForecastable site={{ ...site, reason: visit.reason }} />;
    if (!replay) return <LiveSite key={`${site.id}-${theme}`} site={site} visit={visit} live={live} liveDone={liveDone} forecast={forecast} />;
    if (error) return <p className="p-8 text-alert">Couldn’t load site data: {error}</p>;
    if (!data || data.meta.id !== site.usgsId) return <SiteLoading site={site} />;

    // Charts and the map read colors once when built, so a theme change rebuilds them.
    const key = `${site.id}-${theme}-${clock.at?.getTime() ?? 'live'}`;
    const panel = debug && (
      <TimeTravel data={data} clock={clock} setClock={setClock} issue={issue} theme={theme} setTheme={setTheme} layout={layout} setLayout={setLayout} />
    );
    if (layout === 'editorial') {
      return (
        <>
          <Editorial key={key} data={data} at={clock.at} layer={clock.layer} onIssue={setIssue} />
          {panel}
        </>
      );
    }
    return (
      <div className="mx-auto max-w-6xl px-4 pb-16 sm:px-6">
        <Header key={`h-${key}`} data={data} at={clock.at} />
        <main className="flex flex-col gap-5">
          <Forecast key={key} data={data} at={clock.at} onIssue={setIssue} />
          <SiteMeta meta={data.meta} />
          <Basin key={`b-${site.id}-${theme}`} meta={data.meta} geo={data.geo} at={clock.at} initialLayer={clock.layer} />
        </main>
        <footer className="mt-10 max-w-3xl text-xs leading-relaxed text-faint">
          Forecasts are flowcast’s three-seed LSTM ensemble (132 samples, calibrated), replayed from the held-out validation years WY2021–2022. Live
          conditions: USGS Water Data API and Open-Meteo. Basemap © OpenFreeMap, OpenStreetMap contributors. Basin and rivers: USGS NLDI / NHDPlus V2. Dams: USACE NID. Snowpack:
          NOAA SNODAS. Weather behind each forecast: NOAA GEFS basin mean.
        </footer>
        {panel}
      </div>
    );
  }
}
