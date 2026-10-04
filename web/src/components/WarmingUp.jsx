/** The forecast's place on the page while a site that has no forecast yet warms up. */
export default function WarmingUp({ short, className = '' }) {
  return (
    <section className={`rounded-2xl border border-dashed border-line px-6 py-14 text-center ${className}`}>
      <div className="mx-auto size-2.5 animate-pulse rounded-full bg-flow" />
      <h2 className="mt-4 text-lg font-semibold">The forecast for {short} is warming up</h2>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted">
        flowcast only runs forecasts for rivers people are looking at. Your visit started one; this page fills in by itself when it’s ready.
      </p>
    </section>
  );
}
