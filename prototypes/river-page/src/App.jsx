import { useEffect, useState } from 'react';
import Header from './components/Header.jsx';
import Forecast from './components/Forecast.jsx';
import SiteMeta from './components/SiteMeta.jsx';
import Basin from './components/Basin.jsx';
import TimeTravel from './components/TimeTravel.jsx';
import Editorial from './components/Editorial.jsx';
import SiteBar, { BAR_HEIGHT } from './sites/SiteBar.jsx';
import ForecastWarming from './sites/ForecastWarming.jsx';
import { SiteLoading, SiteNotFound, SitesError } from './sites/SiteStatus.jsx';
import { rememberSite, usgsNumber } from './sites/sites.js';
import { useSiteRoute, useSites, useWarmup } from './sites/useSites.js';
import { loadSite } from './lib/data.js';
import { applyTheme } from './lib/palette.js';

const params = new URLSearchParams(location.search);

function initialClock() {
  const at = params.get('at') ? new Date(params.get('at')) : null;
  return { at: at && !isNaN(at) ? at : null, scenario: params.get('scenario'), layer: params.get('layer') };
}

export default function App() {
  const [siteId, goToSite] = useSiteRoute();
  const { sites, error: sitesError } = useSites();
  const site = sites?.find((s) => s.id === siteId) ?? null;
  const warmup = useWarmup(site);
  const ready = !!site && (site.forecast_ready || !!warmup?.ready);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [clock, setClock] = useState(initialClock);
  const [theme, setThemeName] = useState(() => applyTheme(params.get('theme')));
  const [layout, setLayout] = useState(params.get('layout') === 'cards' ? 'cards' : 'editorial');
  const [issue, setIssue] = useState(null);
  const debug = import.meta.env.DEV;
  const setTheme = (name) => setThemeName(applyTheme(name));
  useEffect(() => {
    if (!site) return;
    rememberSite(site.id);
    document.title = `${site.river} at ${site.town} · flowcast`;
  }, [site]);
  useEffect(() => {
    setData(null);
    setError(null);
    if (!ready) return;
    let stale = false;
    loadSite(usgsNumber(site.id)).then(
      (d) => !stale && setData(d),
      (e) => !stale && setError(String(e)),
    );
    return () => {
      stale = true;
    };
  }, [ready, site?.id]);
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
      <SiteBar key={theme} sites={sites} current={siteId} onSelect={goToSite} />
      <div className={BAR_HEIGHT} aria-hidden />
      {page()}
    </>
  );

  function page() {
    if (sitesError) return <SitesError error={sitesError} />;
    if (!sites) return null;
    if (!site) return <SiteNotFound id={siteId} />;
    if (!ready) return <ForecastWarming key={`${site.id}-${theme}`} site={site} warmup={warmup} />;
    if (error) return <p className="p-8 text-alert">Couldn’t load site data: {error}</p>;
    if (!data || data.meta.id !== usgsNumber(site.id)) return <SiteLoading site={site} />;

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
