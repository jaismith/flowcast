import { fmt } from '../lib/data.js';

export default function SiteMeta({ meta }) {
  const fs = meta.flood_stage_ft;
  const rows = [
    ['Gauge', <a key="g" className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://waterdata.usgs.gov/monitoring-location/USGS-${meta.id}/`} target="_blank" rel="noreferrer">USGS {meta.id}</a>],
    ['NWS point', meta.nws_lid ? <a key="n" className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://water.noaa.gov/gauges/${meta.nws_lid.toLowerCase()}`} target="_blank" rel="noreferrer">{meta.nws_lid}</a> : '—'],
    ['Location', `${meta.lat.toFixed(4)}°N, ${Math.abs(meta.lon).toFixed(4)}°W`],
    ['Typical flow', `${fmt.cfs(meta.median_flow_cfs)} cfs median · ${fmt.cfs(meta.mean_flow_cfs)} mean`],
    ['High flow', `${fmt.cfs(meta.q99_cfs)} cfs (top 1% of hours)`],
    ['Flood stages', fs ? `Action ${fs.action} · Minor ${fs.minor} · Moderate ${fs.moderate} · Major ${fs.major} ft` : '—'],
  ];
  return (
    <section className="card p-5 sm:p-6">
      <div className="eyebrow">The site</div>
      <p className="mt-2 max-w-3xl text-[15px] leading-relaxed text-ink/85">{meta.tagline}</p>
      <dl className="mt-5 grid gap-x-8 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
        {rows.map(([k, v]) => (
          <div key={k} className="border-t border-line pt-2.5">
            <dt className="text-xs text-muted">{k}</dt>
            <dd className="mt-0.5 text-sm font-medium tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
