import { useMemo, useState } from 'react';
import { fmt } from '../lib/data.js';
import { findScenarios, simRange } from '../lib/scenarios.js';

const HOUR = 3600 * 1000;
const pad = (n) => String(n).padStart(2, '0');
const toInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:00`;

/** Debug panel: pretend "now" is any moment in the validation years. */
export default function TimeTravel({ data, clock, setClock }) {
  const [open, setOpen] = useState(true);
  const scenarios = useMemo(() => findScenarios(data), [data]);
  const range = useMemo(() => simRange(data), [data]);
  const groups = useMemo(() => [...new Set(scenarios.map((s) => s.group))], [scenarios]);
  const at = clock.at;

  const go = (d, scenario = null) => {
    const ms = Math.min(range.max.getTime(), Math.max(range.min.getTime(), d.getTime()));
    setClock({ at: new Date(ms), scenario: scenario?.key ?? null, layer: scenario?.layer ?? null });
  };
  const shift = (h) => go(new Date((at ?? range.max).getTime() + h * HOUR));

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className={`fixed right-4 bottom-4 z-50 rounded-full px-4 py-2 text-xs font-semibold shadow-lg ring-1 ${
          at ? 'bg-sun text-ink ring-sun' : 'bg-ink text-white ring-ink'
        }`}
      >
        {at ? `Time travel · ${fmt.date(at)}` : 'Time travel'}
      </button>
    );
  }

  return (
    <aside className="fixed right-4 bottom-4 z-50 flex max-h-[calc(100vh-2rem)] w-80 flex-col overflow-hidden rounded-2xl bg-white/95 text-sm shadow-2xl ring-1 ring-line backdrop-blur">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <div>
          <div className="eyebrow">Debug · time travel</div>
          <div className="mt-0.5 font-semibold">{at ? fmt.when(at) : 'Live (real now)'}</div>
        </div>
        <button onClick={() => setOpen(false)} className="grid size-7 place-items-center rounded-full text-muted hover:bg-paper" aria-label="Minimize">
          –
        </button>
      </div>

      <div className="flex flex-col gap-2 border-b border-line px-4 py-3">
        <input
          type="datetime-local"
          step={3600}
          min={toInput(range.min)}
          max={toInput(range.max)}
          value={toInput(at ?? range.max)}
          onChange={(e) => e.target.value && go(new Date(e.target.value))}
          className="w-full rounded-lg border border-line px-2.5 py-1.5 text-sm tabular-nums"
        />
        <div className="grid grid-cols-5 gap-1">
          {[
            ['−1d', -24],
            ['−6h', -6],
            ['+6h', 6],
            ['+1d', 24],
          ].map(([label, h]) => (
            <button key={label} onClick={() => shift(h)} className="rounded-md border border-line py-1 text-xs font-medium hover:border-faint">
              {label}
            </button>
          ))}
          <button
            onClick={() => setClock({ at: null, scenario: null, layer: null })}
            disabled={!at}
            className="rounded-md bg-ink py-1 text-xs font-medium text-white disabled:opacity-30"
          >
            Live
          </button>
        </div>
        <p className="text-[11px] text-faint">
          {fmt.date(range.min)} – {fmt.date(range.max)} (validation years only)
        </p>
      </div>

      <div className="overflow-y-auto px-2 py-2">
        {groups.map((g) => (
          <div key={g} className="mb-1">
            <div className="eyebrow px-2 pt-2 pb-1">{g}</div>
            {scenarios
              .filter((s) => s.group === g)
              .map((s) => (
                <button
                  key={s.key}
                  onClick={() => go(s.at, s)}
                  className={`block w-full rounded-lg px-2 py-1.5 text-left transition ${clock.scenario === s.key ? 'bg-sun/15' : 'hover:bg-paper'}`}
                >
                  <div className="font-medium">{s.label}</div>
                  <div className="text-xs text-muted">{s.detail}</div>
                </button>
              ))}
          </div>
        ))}
      </div>
    </aside>
  );
}
