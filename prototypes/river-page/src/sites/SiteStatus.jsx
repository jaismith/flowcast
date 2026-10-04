import { canonicalId } from './sites.js';

/** The river page's title block while a site's forecast files load, so the header doesn't jump when they land. */
export function SiteLoading({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site.river} <span className="font-normal text-muted">at {site.town}, {site.state}</span>
      </h1>
      <div className="mt-6 border-t border-line pt-6 text-[13px] text-muted">Loading the forecast…</div>
    </Shell>
  );
}

export function SiteNotFound({ id }) {
  const usgs = canonicalId(id);
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">No forecast for {usgs ?? `“${id}”`} yet</h1>
      <p className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        {usgs ? 'flowcast doesn’t cover this gauge. ' : 'That isn’t a USGS gauge number. '}
        Search above by river, town or USGS number, or open the map to see every gauge flowcast covers.
        {usgs && (
          <>
            {' '}
            <a className="underline decoration-line underline-offset-2 hover:decoration-ink" href={`https://waterdata.usgs.gov/monitoring-location/${usgs}/`} target="_blank" rel="noreferrer">
              USGS gauge page
            </a>
          </>
        )}
      </p>
    </Shell>
  );
}

export function SitesError({ error }) {
  return (
    <Shell>
      <p className="text-alert">Couldn’t load the list of gauges: {error}</p>
    </Shell>
  );
}

function Shell({ children }) {
  return <div className="mx-auto max-w-5xl px-5 pt-10 pb-20 sm:px-8 sm:pt-14">{children}</div>;
}
