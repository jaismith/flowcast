import { useEffect, useRef, useState } from 'react';

export function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/**
 * Renders `build(width)` (a Plot.plot call) into a div that tracks its own width.
 *
 * For a hover card, the plot needs exactly one pointer mark (e.g. a ruleX with Plot.pointerX); `tip.at(value)`
 * returns the [x, y] data position to anchor at and `tip.render(value)` the card's contents.
 */
export default function PlotFigure({ build, deps, className = '', tip }) {
  const [ref, width] = useWidth();
  const host = useRef(null);
  const [hover, setHover] = useState(null);
  useEffect(() => {
    if (!width) return;
    const el = build(width);
    host.current.replaceChildren(el);
    setHover(null);
    if (!tip) return () => el.remove();
    const onInput = () => {
      const v = el.value;
      if (v == null) return setHover(null);
      const [x, y] = tip.at(v);
      setHover({ v, x: el.scale('x').apply(x), y: el.scale('y').apply(y) });
    };
    el.addEventListener('input', onInput);
    return () => {
      el.removeEventListener('input', onInput);
      el.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, ...deps]);

  const below = hover && hover.y < 90;
  const left = hover && Math.min(Math.max(hover.x, 90), width - 90);
  return (
    <div ref={ref} className={`relative ${className}`}>
      <div ref={host} />
      {hover && tip && (
        <>
          <div
            className="pointer-events-none absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card bg-ink shadow"
            style={{ left: hover.x, top: hover.y }}
          />
          <div
            className="pointer-events-none absolute z-10 min-w-40 rounded-lg bg-card px-3 py-2 text-[12px] shadow-lg ring-1 ring-line"
            style={{ left, top: below ? hover.y + 14 : hover.y - 14, transform: `translate(-50%, ${below ? '0' : '-100%'})` }}
          >
            {tip.render(hover.v)}
          </div>
        </>
      )}
    </div>
  );
}

/** Building blocks for hover cards: a bold heading and label/value rows with an optional swatch. */
export function TipHead({ children }) {
  return <div className="mb-1 font-semibold whitespace-nowrap text-ink">{children}</div>;
}

export function TipRow({ swatch, label, value, dotted }) {
  return (
    <div className="flex items-center justify-between gap-4 py-px whitespace-nowrap">
      <span className="flex items-center gap-1.5 text-muted">
        {swatch && (
          <span
            className="inline-block h-2 w-3 rounded-[2px]"
            style={dotted ? { backgroundImage: `radial-gradient(circle, ${swatch} 1px, transparent 1.3px)`, backgroundSize: '4px 4px' } : { background: swatch }}
          />
        )}
        {label}
      </span>
      <span className="font-medium text-ink tabular-nums">{value}</span>
    </div>
  );
}
