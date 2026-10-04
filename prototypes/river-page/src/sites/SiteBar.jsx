import { useEffect, useMemo, useRef, useState } from 'react';
import SiteMap from './SiteMap.jsx';
import { searchSites, usgsNumber } from './sites.js';
import { fmt } from '../lib/data.js';

/** Height of the fixed bar; pages put a spacer of this height above their content. */
export const BAR_HEIGHT = 'h-14';

/**
 * The fixed bar at the top of every page: where you are, quick search, and a map of every gauge. The finder
 * opens over the page rather than pushing it down, so nothing below moves.
 */
export default function SiteBar({ sites, current, onSelect }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [mapMounted, setMapMounted] = useState(false);
  const [active, setActive] = useState(0);
  const [hover, setHover] = useState(null);
  const input = useRef(null);
  const rows = useRef(new Map());
  const here = sites?.find((s) => s.id === current) ?? null;
  const results = useMemo(() => (sites ? searchSites(sites, query, here) : []), [sites, query, here]);

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
    }
  };

  return (
    <>
      <div
        className={`fixed inset-0 z-30 bg-ink/10 transition-opacity duration-150 ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
        onClick={hide}
        aria-hidden
      />
      <div className="fixed inset-x-0 top-0 z-40">
        <div className="border-b border-line bg-paper/90 backdrop-blur-md">
          <div className={`mx-auto flex ${BAR_HEIGHT} max-w-5xl items-center gap-3 px-5 sm:px-8`}>
            <span className="text-[15px] font-semibold tracking-tight text-flow">flowcast</span>
            <span className="hidden min-w-0 items-center gap-3 text-sm text-muted sm:flex">
              <span className="text-line">/</span>
              <span className="truncate">{here ? `${here.river} at ${here.town}` : ''}</span>
            </span>
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

        <div
          className={`mx-auto max-w-5xl px-3 transition duration-150 ease-out sm:px-6 ${open ? 'translate-y-0 opacity-100' : 'pointer-events-none -translate-y-1 opacity-0'}`}
          aria-hidden={!open}
        >
          <div className="mt-2 grid h-[min(560px,calc(100dvh-5rem))] grid-rows-[180px_minmax(0,1fr)] overflow-hidden rounded-xl bg-card shadow-2xl ring-1 ring-line md:grid-cols-[340px_1fr] md:grid-rows-1">
            <div className="order-2 flex min-h-0 flex-col md:order-1 md:border-r md:border-line">
              <div className="flex items-baseline justify-between border-b border-line px-4 py-2.5 text-[12px] text-muted">
                <span>{query.trim() ? `${results.length} ${results.length === 1 ? 'gauge' : 'gauges'}` : here ? `Nearest ${here.town}` : 'All gauges'}</span>
                <span className="hidden text-faint sm:inline">↑↓ to move · ↵ to open</span>
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
                      className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left ${i === active ? 'bg-ink/[0.05]' : ''}`}
                    >
                      <Dot ready={s.forecast_ready} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{s.river}</span>
                        <span className="block truncate text-[12.5px] text-muted">
                          {s.place} · {fmt.int(s.area_mi2)} mi²
                        </span>
                      </span>
                      <span className="shrink-0 text-right leading-tight">
                        <span className="block font-mono text-[11px] text-faint">{usgsNumber(s.id)}</span>
                        {s.id === current && <span className="text-[11px] font-medium text-flow">Viewing</span>}
                      </span>
                    </button>
                  </li>
                ))}
                {sites && !results.length && (
                  <li className="px-3 py-6 text-sm text-muted">
                    No gauges match “{query.trim()}”. Try a river, a town, or a USGS number.
                  </li>
                )}
              </ul>
              <div className="flex gap-4 border-t border-line px-4 py-2 text-[11.5px] text-muted">
                <span className="flex items-center gap-1.5">
                  <Dot ready /> Forecast ready
                </span>
                <span className="flex items-center gap-1.5">
                  <Dot /> Forecast starts when opened
                </span>
              </div>
            </div>
            <div className="relative order-1 bg-normal md:order-2">
              {mapMounted && sites && (
                <SiteMap sites={sites} matches={results} current={current} hover={hover} onHover={hoverFromMap} onSelect={select} open={open} />
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function Dot({ ready }) {
  return <span className={`size-2.5 shrink-0 rounded-full ${ready ? 'bg-flow' : 'bg-card ring-2 ring-flow ring-inset'}`} />;
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 16 16" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" aria-hidden>
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
