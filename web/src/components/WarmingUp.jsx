/** The forecast's place on the page while a site with no forecast yet warms up (or can't, while paused). */
export default function WarmingUp({ short, paused = false, className = '' }) {
  return (
    <section className={`rounded-2xl border border-dashed border-line px-6 py-14 text-center ${className}`}>
      <div className={`mx-auto size-2.5 rounded-full ${paused ? 'bg-faint' : 'animate-pulse bg-flow'}`} />
      <h2 className="mt-4 text-lg font-semibold">{paused ? `Live forecasts for ${short} are paused` : `The forecast for ${short} is warming up`}</h2>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted">
        {paused
          ? 'flowcast is running as many rivers as it can right now. Gauge readings above stay current; check back later for the forecast.'
          : 'flowcast only runs forecasts for rivers people are looking at. Your visit started one; this page fills in by itself when it’s ready.'}
      </p>
    </section>
  );
}
