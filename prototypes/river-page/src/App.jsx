import { useEffect, useState } from 'react';
import Header from './components/Header.jsx';
import Forecast from './components/Forecast.jsx';
import SiteMeta from './components/SiteMeta.jsx';
import Basin from './components/Basin.jsx';
import TimeTravel from './components/TimeTravel.jsx';
import Editorial from './components/Editorial.jsx';
import { loadSite } from './lib/data.js';
import { applyTheme } from './lib/palette.js';

const params = new URLSearchParams(location.search);
const SITE = params.get('site') ?? '01427510';

function initialClock() {
  const at = params.get('at') ? new Date(params.get('at')) : null;
  return { at: at && !isNaN(at) ? at : null, scenario: params.get('scenario'), layer: params.get('layer') };
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [clock, setClock] = useState(initialClock);
  const [theme, setThemeName] = useState(() => applyTheme(params.get('theme')));
  const [layout, setLayout] = useState(params.get('layout') === 'cards' ? 'cards' : 'editorial');
  const [basemap, setBasemap] = useState(params.get('map'));
  const [issue, setIssue] = useState(null);
  const debug = import.meta.env.DEV;
  const setTheme = (name) => setThemeName(applyTheme(name));
  useEffect(() => {
    loadSite(SITE).then(setData, (e) => setError(String(e)));
  }, []);
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    for (const [k, v] of [
      ['at', clock.at?.toISOString()],
      ['scenario', clock.scenario],
      ['layer', clock.layer],
      ['theme', theme === 'modern' ? null : theme],
      ['layout', layout === 'editorial' ? null : layout],
      ['map', basemap],
    ]) {
      if (v) p.set(k, v);
      else p.delete(k);
    }
    history.replaceState(null, '', `${location.pathname}${p.size ? `?${p}` : ''}`);
  }, [clock, theme, layout, basemap]);

  if (error) return <p className="p-8 text-alert">Couldn’t load site data: {error}</p>;
  if (!data) return <p className="p-8 text-muted">Loading…</p>;

  // Charts and the map read colors once when built, so a theme change rebuilds them.
  const key = `${theme}-${clock.at?.getTime() ?? 'live'}`;
  const panel = debug && (
    <TimeTravel data={data} clock={clock} setClock={setClock} issue={issue} theme={theme} setTheme={setTheme} layout={layout} setLayout={setLayout} basemap={basemap} setBasemap={setBasemap} />
  );
  if (layout === 'editorial') {
    return (
      <>
        <Editorial key={key} data={data} at={clock.at} layer={clock.layer} basemap={basemap} onIssue={setIssue} />
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
        <Basin key={`b-${theme}`} meta={data.meta} geo={data.geo} at={clock.at} initialLayer={clock.layer} basemap={basemap} />
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
