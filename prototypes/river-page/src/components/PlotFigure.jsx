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

/** Renders `build(width)` (a Plot.plot call) into a div that tracks its own width. */
export default function PlotFigure({ build, deps, className = '' }) {
  const [ref, width] = useWidth();
  useEffect(() => {
    if (!width) return;
    const el = build(width);
    ref.current.replaceChildren(el);
    return () => el.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, ...deps]);
  return <div ref={ref} className={className} />;
}
