import { Loader, Skeleton } from './Skeleton.jsx';
import { canonicalId, placeOf } from './sites.js';

const link = 'underline decoration-line underline-offset-2 hover:decoration-ink';

/** The river page's frame in shimmer while a site resolves or its forecast files load, with one loader over the chart. */
export function SiteLoading({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site ? (
          <>
            {site.river} {site.town && <span className="font-normal text-muted">at {placeOf(site)}</span>}
          </>
        ) : (
          <Skeleton className="h-9 w-[28rem] max-w-full sm:h-10" />
        )}
      </h1>
      <div className="mt-6 grid gap-x-12 gap-y-8 border-t border-line pt-6 md:grid-cols-[1fr_1fr_1.1fr]" aria-hidden>
        {[0, 1, 2].map((i) => (
          <div key={i}>
            <Skeleton className="h-3 w-16" />
            <Skeleton className="mt-3 h-4 w-32" />
            <Skeleton className="mt-2.5 h-9 w-40" />
            <Skeleton className="mt-3 h-3 w-28" />
          </div>
        ))}
      </div>
      <div className="relative mt-16">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="mt-6 h-[386px] w-full opacity-60 sm:h-[440px]" />
        <div className="absolute inset-0 top-11 grid place-items-center">
          <Loader />
        </div>
      </div>
    </Shell>
  );
}

/** A real USGS stream gauge that flowcast can't forecast, and why. */
export function GaugeNotForecastable({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site.river} {site.town && <span className="font-normal text-muted">at {placeOf(site)}</span>}
      </h1>
      <div className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        <p className="font-medium text-ink">flowcast can’t forecast this gauge.</p>
        <p className="mt-1">
          {site.reason}. Both forecasts start from the gauge’s latest flow reading, so a gauge needs live flow data. Search above or open the map to find
          a gauge nearby.{' '}
          <a className={link} href={`https://waterdata.usgs.gov/monitoring-location/${site.id}/`} target="_blank" rel="noreferrer">
            USGS gauge page
          </a>
        </p>
      </div>
    </Shell>
  );
}

export function SiteNotFound({ id }) {
  const usgs = canonicalId(id);
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{usgs ? `No USGS stream gauge ${usgs.replace('USGS-', '')}` : `No gauge called “${id}”`}</h1>
      <p className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        {usgs ? 'USGS doesn’t list an active stream gauge with that number. ' : 'That isn’t a USGS gauge number or a flowcast site. '}
        Search above by river, town or USGS number, or open the map to browse every gauge.
      </p>
    </Shell>
  );
}

export function GaugeLookupError({ id, error }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Couldn’t look up USGS {id.replace('USGS-', '')}</h1>
      <p className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">{error}. Reload in a few minutes to try again.</p>
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
