import { fmt } from '../lib/data.js';
import { floodLevels } from '../lib/rating.js';
import FloodBar from './FloodBar.jsx';

const link = 'underline decoration-line underline-offset-2 hover:decoration-ink';

export default function SiteMeta({ meta }) {
  const levels = floodLevels(meta);
  const rows = [
    ['Gauge', <a key="g" className={link} href={`https://waterdata.usgs.gov/monitoring-location/USGS-${meta.id}/`} target="_blank" rel="noreferrer">USGS {meta.id}</a>],
    ['NWS point', meta.nws_lid ? <a key="n" className={link} href={`https://water.noaa.gov/gauges/${meta.nws_lid.toLowerCase()}`} target="_blank" rel="noreferrer">{meta.nws_lid}</a> : '—'],
    ['Location', `${meta.lat.toFixed(4)}°N, ${Math.abs(meta.lon).toFixed(4)}°W`],
    ['Typical flow', `${fmt.cfs(meta.median_flow_cfs)} cfs median · ${fmt.cfs(meta.mean_flow_cfs)} mean`],
    ['High flow', `${fmt.cfs(meta.q99_cfs)} cfs (top 1% of hours)`],
  ];
  return (
    <section className="card p-5 sm:p-6">
      <div className="eyebrow">The site</div>
      <p className="mt-2 max-w-3xl text-[15px] leading-relaxed text-ink/85">{meta.tagline}</p>
      <div className="mt-5 grid gap-x-10 gap-y-5 lg:grid-cols-[1fr_1fr]">
        <dl className="grid grid-cols-2 gap-x-8 gap-y-3">
          {rows.map(([k, v]) => (
            <div key={k} className="border-t border-line pt-2.5">
              <dt className="text-xs text-muted">{k}</dt>
              <dd className="mt-0.5 text-sm font-medium tabular-nums">{v}</dd>
            </div>
          ))}
        </dl>
        {levels.length > 0 && (
          <div className="border-t border-line pt-2.5">
            <div className="text-xs text-muted">Flood stages (NWS)</div>
            <div className="mt-3">
              <FloodBar levels={levels} ticks />
            </div>
            <ul className="mt-1 grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
              {levels.map((l) => (
                <li key={l.key} className="flex items-baseline gap-2">
                  <span className="size-2.5 shrink-0 translate-y-px rounded-sm" style={{ background: l.color }} />
                  <span className="font-medium">{l.label}</span>
                  <span className="text-muted tabular-nums">
                    {l.ft} ft{l.cfs ? ` ≈ ${fmt.cfs(l.cfs)} cfs` : ''}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-[11px] text-faint">
              Action means the river is high enough that the NWS starts watching closely. Flows are from today’s USGS rating.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
