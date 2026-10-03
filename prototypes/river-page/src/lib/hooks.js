import { useEffect, useMemo, useState } from 'react';
import { archivedGauge, forecastAt, liveGauge, presets, replayIssues } from './data.js';
import { issueAtOrBefore } from './scenarios.js';

/** Gauge readings "now": live from USGS, or from the archive when `at` is a simulated time. */
export function useGauge(data, at) {
  const [fetched, setFetched] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!at) liveGauge(data.meta.id).then(setFetched, () => setError(true));
  }, [data.meta.id, at]);
  const archived = useMemo(() => at && archivedGauge(data.observed, at), [data.observed, at]);
  return { gauge: at ? archived : fetched, error };
}

/** The forecast run being shown, stepping through regular issues with ← / →. */
export function useReplay(data, at, onIssue) {
  const hc = data.hindcast;
  const list = useMemo(() => replayIssues(hc), [hc]);
  const start = useMemo(() => (at ? issueAtOrBefore(hc, at) : presets(hc, list)[0].idx), [hc, list, at]);
  const [idx, setIdx] = useState(start);
  useEffect(() => setIdx(start), [start]);
  const f = useMemo(() => forecastAt(data, idx), [data, idx]);
  useEffect(() => onIssue?.(f.issue), [f.issue, onIssue]);

  const pos = list.indexOf(idx);
  const step = (d) => setIdx(list[Math.min(list.length - 1, Math.max(0, pos + d))]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea, select')) return;
      if (e.key === 'ArrowLeft') step(-1);
      if (e.key === 'ArrowRight') step(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  return { f, idx, setIdx, step, first: pos <= 0, last: pos >= list.length - 1, isNow: idx === start };
}
