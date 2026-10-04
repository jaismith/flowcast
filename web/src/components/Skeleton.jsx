import { C } from '../lib/palette.js';

if (!document.getElementById('fc-shimmer')) {
  const style = Object.assign(document.createElement('style'), { id: 'fc-shimmer' });
  style.textContent = '@keyframes fc-shimmer{from{background-position:150% 0}to{background-position:-50% 0}}';
  document.head.append(style);
}

/** A text-free placeholder block with a slow shimmer. Size it with className. */
export function Skeleton({ className = '' }) {
  return (
    <span
      aria-hidden
      className={`block rounded-md ${className}`}
      style={{
        background: `linear-gradient(90deg, ${C.normal} 25%, ${C.paper} 50%, ${C.normal} 75%)`,
        backgroundSize: '200% 100%',
        animation: 'fc-shimmer 1.6s ease-in-out infinite',
      }}
    />
  );
}

/**
 * The one loader on a page: a ring that fills toward `progress` (0–1) or spins when there's no estimate, with a
 * short label under it.
 */
export function Loader({ progress = null, label, detail, stalled = false }) {
  const r = 18;
  const len = 2 * Math.PI * r;
  const known = progress != null && !stalled;
  return (
    <div className="flex flex-col items-center text-center" role="status" aria-live="polite">
      <svg viewBox="0 0 44 44" className={`size-11 ${known || stalled ? '' : 'animate-spin'}`} aria-hidden>
        <circle cx="22" cy="22" r={r} fill="none" stroke={C.line} strokeWidth="3.5" />
        <circle
          cx="22"
          cy="22"
          r={r}
          fill="none"
          stroke={stalled ? C.faint : C.flow}
          strokeWidth="3.5"
          strokeLinecap="round"
          strokeDasharray={`${(known ? Math.max(0.08, progress) : stalled ? 1 : 0.28) * len} ${len}`}
          transform="rotate(-90 22 22)"
          style={{ transition: 'stroke-dasharray 1s linear' }}
        />
      </svg>
      {label && <div className="mt-3 text-sm font-semibold text-ink">{label}</div>}
      {detail && <div className="mt-0.5 text-[12.5px] text-muted">{detail}</div>}
    </div>
  );
}
