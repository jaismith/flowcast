import { useEffect, useMemo, useRef, useState } from 'react';
import SiteMap, { kindOf } from './SiteMap.jsx';
import { byDistance, milesBetween, placeOf, searchSites, titleOf } from '../lib/search.js';
import { canonicalId } from '../lib/router.js';
import { GAUGE_MIN_ZOOM, useGaugesInView } from '../lib/usePlaces.js';
import { fmt } from '../lib/data.js';

/** Height of the fixed bar; pages put a spacer of this height above their content. */
export const BAR_HEIGHT = 'h-14';
const MAX_ROWS = 80;

/**
 * The fixed bar at the top of every page: where you are, quick search, and a map of every gauge. The finder opens
 * over the page rather than pushing it down, so nothing below moves. With an empty search the list follows the map.
 */
export default function SiteBar({ sites, current, onSelect }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [mapMounted, setMapMounted] = useState(false);
  const [active, setActive] = useState(0);
  const [hover, setHover] = useState(null);
  const [view, setView] = useState(null);
  const [flyTo, setFlyTo] = useState(null);
  const input = useRef(null);
  const rows = useRef(new Map());
  const header = useRef(null);
  const { gauges, loading, error } = useGaugesInView(open ? view : null);

  const all = useMemo(() => {
    if (!sites) return [];
    const ids = new Set(sites.map((s) => s.id));
    return [...sites, ...gauges.filter((g) => !ids.has(g.id))];
  }, [sites, gauges]);
  const q = query.trim();
  const inView = useMemo(() => {
    if (!view) return null;
    const [w, s, e, n] = view.bounds;
    const center = { lat: view.center.lat, lon: view.center.lng };
    const visible = all.filter((x) => x.lon >= w && x.lon <= e && x.lat >= s && x.lat <= n);
    return byDistance(visible, center).sort((a, b) => b.forecastable - a.forecastable);
  }, [all, view]);
  const results = useMemo(() => {
    if (!sites) return [];
    if (q) return searchSites(all, q, current).slice(0, MAX_ROWS);
    return (inView ?? byDistance(sites, current ?? sites[0])).slice(0, MAX_ROWS);
  }, [sites, all, q, current, inView]);
  const typedId = canonicalId(q);
  const lookup = typedId && !results.some((s) => s.id === typedId) ? typedId : null;
  const matchIds = useMemo(() => (q ? new Set(results.map((s) => s.id)) : null), [q, results]);
  const fit = useMemo(() => ({ key: q, sites: q ? results.slice(0, 20) : [] }), [q]);
  const forecastableInView = inView?.filter((s) => s.forecastable).length ?? 0;
  const nearest = useMemo(() => {
    if (!view || forecastableInView || loading || error) return null;
    const c = { lat: view.center.lat, lon: view.center.lng };
    const best = byDistance(
      all.filter((s) => s.forecastable),
      c,
    )[0];
    return best ? { site: best, mi: milesBetween(best, c) } : null;
  }, [view, forecastableInView, loading, all]);

  const show = () => {
    setOpen(true);
    setMapMounted(true);
  };
  const hide = () => {
    setOpen(false);
    setHover(null);
    input.current?.blur();
  };
  const select = (id) => {
    hide();
    setQuery('');
    onSelect(id);
  };

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    const onKey = (e) => {
      const typing = e.target.closest?.('input, textarea, select');
      if (!typing && (e.key === '/' || (e.key === 'k' && (e.metaKey || e.ctrlKey)))) {
        e.preventDefault();
        input.current.focus();
      } else if (e.key === 'Escape') hide();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const hoverFromMap = (id) => {
    setHover(id);
    const i = results.findIndex((s) => s.id === id);
    if (i >= 0) {
      setActive(i);
      rows.current.get(id)?.scrollIntoView({ block: 'nearest' });
    }
  };
  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const i = Math.max(0, Math.min(results.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
      setActive(i);
      setHover(results[i]?.id ?? null);
      rows.current.get(results[i]?.id)?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && results[active]) {
      select(results[active].id);
    } else if (e.key === 'Enter' && lookup) {
      select(lookup);
    }
  };

  const heading = q
    ? `${results.length} ${results.length === 1 ? 'gauge' : 'gauges'}`
    : inView
      ? `${forecastableInView} forecastable in view`
      : current
        ? `Nearest ${current.town ?? current.river}`
        : 'All gauges';

  return (
    <>
      <div
        className={`fixed inset-0 z-30 bg-ink/10 transition-opacity duration-150 ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
        onClick={hide}
        aria-hidden
      />
      {/* The container spans the finder's box, so only the bar and the open finder take pointer events. Tabbing past
          both into the page closes the finder. */}
      <div
        ref={header}
        className="pointer-events-none fixed inset-x-0 top-0 z-40"
        onBlur={(e) => open && e.relatedTarget && !header.current.contains(e.relatedTarget) && hide()}
      >
        <div className="pointer-events-auto border-b border-line bg-paper/90 backdrop-blur-md">
          <div className={`mx-auto flex ${BAR_HEIGHT} max-w-5xl items-center gap-3 px-5 sm:px-8`}>
            <span className="text-[15px] font-semibold tracking-tight text-flow">flowcast</span>
            {current && (
              <span className="hidden min-w-0 items-center gap-3 text-sm text-muted sm:flex">
                <span className="text-line">/</span>
                <span className="truncate">{titleOf(current)}</span>
              </span>
            )}
            <div className="relative ml-auto w-full max-w-[22rem] min-w-0">
              <SearchIcon />
              <input
                ref={input}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  show();
                }}
                onFocus={show}
                onKeyDown={onKeyDown}
                placeholder="River, town or USGS number"
                aria-label="Find a river gauge"
                role="combobox"
                aria-expanded={open}
                aria-controls="site-results"
                aria-activedescendant={open && results[active] ? `site-${results[active].id}` : undefined}
                autoComplete="off"
                spellCheck={false}
                className="h-9 w-full rounded-lg bg-card pr-9 pl-9 text-sm ring-1 ring-line outline-none placeholder:text-faint focus:ring-2 focus:ring-flow/40"
              />
              <kbd
                className={`pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 rounded border border-line px-1.5 font-sans text-[11px] text-faint transition-opacity ${open ? 'opacity-0' : ''}`}
              >
                /
              </kbd>
            </div>
            <button
              onClick={() => (open ? hide() : (show(), input.current.focus()))}
              aria-pressed={open}
              className={`inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg px-3 text-sm ring-1 transition-colors ${
                open ? 'bg-ink text-card ring-ink' : 'bg-card text-ink ring-line hover:bg-ink/[0.04]'
              }`}
            >
              <MapIcon />
              <span className="hidden sm:inline">Map</span>
            </button>
          </div>
        </div>

        {/* Closed, the finder stays mounted (the map keeps its state) but is invisible and inert: no clicks, focus or screen reader. */}
        <div
          className={`mx-auto max-w-5xl px-3 transition-[opacity,translate,visibility] duration-150 ease-out sm:px-6 ${
            open ? 'pointer-events-auto visible translate-y-0 opacity-100' : 'invisible -translate-y-1 opacity-0'
          }`}
          inert={!open}
        >
          <div className="mt-2 grid h-[min(600px,calc(100dvh-5rem))] grid-rows-[200px_minmax(0,1fr)] overflow-hidden rounded-xl bg-card shadow-2xl ring-1 ring-line md:grid-cols-[340px_1fr] md:grid-rows-1">
            <div className="order-2 flex min-h-0 flex-col md:order-1 md:border-r md:border-line">
              <div className="flex items-baseline justify-between border-b border-line px-4 py-2.5 text-[12px] text-muted">
                <span>{heading}</span>
                <span className="hidden text-faint sm:inline">↑↓ ↵ to open</span>
              </div>
              <ul id="site-results" role="listbox" className="min-h-0 flex-1 overflow-y-auto p-1.5">
                {results.map((s, i) => (
                  <li key={s.id} ref={(el) => (el ? rows.current.set(s.id, el) : rows.current.delete(s.id))}>
                    <button
                      id={`site-${s.id}`}
                      role="option"
                      aria-selected={i === active}
                      tabIndex={-1}
                      onMouseEnter={() => {
                        setActive(i);
                        setHover(s.id);
                      }}
                      onClick={() => select(s.id)}
                      className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left ${i === active ? 'bg-ink/[0.05]' : ''} ${s.forecastable ? '' : 'text-muted'}`}
                    >
                      <Dot kind={kindOf(s)} />
                      <span className="min-w-0 flex-1">
                        <span className={`block truncate text-sm ${s.forecastable ? 'font-medium' : ''}`}>{s.river}</span>
                        <span className="block truncate text-[12.5px] text-muted">{rowDetail(s)}</span>
                      </span>
                      <span className="shrink-0 text-right leading-tight">
                        <span className="block font-mono text-[11px] text-faint">{s.usgsId}</span>
                        {s.id === current?.id && <span className="text-[11px] font-medium text-flow">Viewing</span>}
                      </span>
                    </button>
                  </li>
                ))}
                {lookup && (
                  <li>
                    <button onClick={() => select(lookup)} className="flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm hover:bg-ink/[0.05]">
                      <SearchIcon inline />
                      Look up USGS gauge <span className="font-mono text-[12.5px]">{lookup.replace('USGS-', '')}</span>
                    </button>
                  </li>
                )}
                {sites && !results.length && !lookup && (
                  <li className="px-3 py-6 text-sm text-muted">
                    {q ? `No gauges match “${q}”. Try a river, a town, or a USGS number.` : 'No gauges in view.'}
                  </li>
                )}
              </ul>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 border-t border-line px-4 py-2 text-[11.5px] text-muted">
                {LEGEND.map(([kind, label]) => (
                  <span key={kind} className="flex items-center gap-1.5">
                    <Dot kind={kind} /> {label}
                  </span>
                ))}
              </div>
            </div>
            <div className="relative order-1 bg-normal md:order-2">
              {mapMounted && sites && (
                <SiteMap
                  sites={all}
                  matchIds={matchIds}
                  current={current?.id}
                  start={current}
                  hover={hover}
                  onHover={hoverFromMap}
                  onSelect={select}
                  onView={setView}
                  open={open}
                  fit={fit}
                  flyTo={flyTo}
                />
              )}
              <MapNote
                loading={loading}
                error={error}
                zoomedOut={view && view.zoom < GAUGE_MIN_ZOOM}
                nearest={!q && nearest}
                onShow={(s) => {
                  setFlyTo({ lat: s.lat, lon: s.lon, at: Date.now() });
                  setHover(s.id);
                }}
              />
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

const LEGEND = [
  ['ready', 'Forecast ready'],
  ['start', 'Forecast starts when opened'],
  ['flow', 'Flow only, no water temperature'],
  ['none', 'Can’t forecast'],
];

function rowDetail(s) {
  const place = placeOf(s);
  if (!s.forecastable) return [place, s.reason].filter(Boolean).join(' · ');
  const area = s.areaMi2 != null ? `${fmt.int(s.areaMi2)} mi²` : null;
  return [place, area, s.temperature ? null : 'flow only'].filter(Boolean).join(' · ');
}

/** One line over the map: loading, why nothing is clickable here, and where the nearest forecastable gauge is. */
function MapNote({ loading, error, zoomedOut, nearest, onShow }) {
  let body = null;
  if (zoomedOut) body = 'Zoom in to see every USGS gauge.';
  else if (error) body = `Couldn’t load USGS gauges here: ${error}.`;
  else if (loading) body = 'Loading USGS gauges…';
  else if (nearest) {
    body = (
      <>
        No forecastable gauges here. Nearest: <span className="font-medium text-ink">{titleOf(nearest.site)}</span>
        {nearest.site.region ? `, ${nearest.site.region}` : ''} · {Math.round(nearest.mi)} mi
        <button onClick={() => onShow(nearest.site)} className="ml-2 rounded-md bg-ink px-2 py-0.5 text-[12px] font-medium text-card hover:bg-ink/85">
          Show
        </button>
      </>
    );
  }
  return (
    <div
      className={`pointer-events-none absolute inset-x-3 bottom-8 flex justify-center transition-opacity duration-150 ${body ? 'opacity-100' : 'opacity-0'}`}
      aria-live="polite"
    >
      {body && <div className="pointer-events-auto max-w-full rounded-lg bg-card/95 px-3 py-2 text-[12.5px] text-muted shadow-md ring-1 ring-line backdrop-blur">{body}</div>}
    </div>
  );
}

function Dot({ kind }) {
  const cls = {
    ready: 'size-2.5 bg-flow',
    start: 'size-2.5 bg-card ring-2 ring-flow ring-inset',
    flow: 'size-2.5 bg-card ring-[1.6px] ring-flow/50 ring-inset',
    none: 'mx-0.5 size-1.5 bg-faint',
  }[kind];
  return <span className={`shrink-0 rounded-full ${cls}`} />;
}

function SearchIcon({ inline = false }) {
  return (
    <svg viewBox="0 0 16 16" className={`pointer-events-none size-4 text-faint ${inline ? 'shrink-0' : 'absolute top-1/2 left-3 -translate-y-1/2'}`} aria-hidden>
      <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="m10.5 10.5 3 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function MapIcon() {
  return (
    <svg viewBox="0 0 16 16" className="size-4" aria-hidden>
      <path
        d="M1.8 3.6 5.6 2l4.8 1.8L14.2 2v10.4l-3.8 1.6-4.8-1.8-3.8 1.6V3.6Z M5.6 2v10.2 M10.4 3.8V14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}
