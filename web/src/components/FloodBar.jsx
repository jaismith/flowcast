import { C } from '../lib/palette.js';

/** River level on the NWS flood-category scale: gray below action, then action / minor / moderate / major. */
export default function FloodBar({ levels, ft, ticks = false }) {
  if (!levels.length) return null;
  const lo = Math.min(0, ft ?? 0);
  const hi = levels.at(-1).ft + 3;
  const frac = (v) => (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo);
  const x = (v) => `${100 * frac(v)}%`;
  const edges = [lo, ...levels.map((l) => l.ft), hi];
  const colors = [C.normal, ...levels.map((l) => l.color)];
  const tickRow = ticks && (
    <div className="relative h-4">
      {levels.map((l) => (
        <div key={l.key} className="absolute top-1 -translate-x-1/2 text-[10px] text-muted tabular-nums" style={{ left: x(l.ft) }}>
          {l.ft}
        </div>
      ))}
    </div>
  );

  return (
    <div className="relative w-full">
      <div className="relative h-1.5 overflow-hidden rounded-full">
        {colors.map((c, i) => (
          <div key={i} className="absolute inset-y-0" style={{ left: x(edges[i]), width: `calc(${x(edges[i + 1])} - ${x(edges[i])})`, background: c }} />
        ))}
      </div>
      {ft != null && (
        <div className="absolute -top-1 h-3.5 w-[3px] -translate-x-1/2 rounded-full bg-ink ring-2 ring-card" style={{ left: x(ft) }} title={`${ft.toFixed(1)} ft`} />
      )}
      {tickRow}
    </div>
  );
}
