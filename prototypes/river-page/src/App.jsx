import { useEffect, useState } from 'react';
import Header from './components/Header.jsx';
import Forecast from './components/Forecast.jsx';
import SiteMeta from './components/SiteMeta.jsx';
import Basin from './components/Basin.jsx';
import { loadSite } from './lib/data.js';

const SITE = new URLSearchParams(location.search).get('site') ?? '01427510';

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    loadSite(SITE).then(setData, (e) => setError(String(e)));
  }, []);

  if (error) return <p className="p-8 text-alert">Couldn’t load site data: {error}</p>;
  if (!data) return <p className="p-8 text-muted">Loading…</p>;

  return (
    <div className="mx-auto max-w-6xl px-4 pb-16 sm:px-6">
      <Header meta={data.meta} clim={data.clim} />
      <main className="flex flex-col gap-5">
        <Forecast data={data} />
        <SiteMeta meta={data.meta} />
        <Basin meta={data.meta} geo={data.geo} />
      </main>
      <footer className="mt-10 max-w-3xl text-xs leading-relaxed text-faint">
        Forecasts are flowcast’s three-seed LSTM ensemble (132 samples, calibrated), replayed from the held-out validation years WY2021–2022. Live
        conditions: USGS Water Data API and Open-Meteo. Basemap © OpenFreeMap, OpenStreetMap contributors. Basin and rivers: USGS NLDI / NHDPlus V2. Dams: USACE NID. Snowpack:
        NOAA SNODAS. Weather behind each forecast: NOAA GEFS basin mean.
      </footer>
    </div>
  );
}
