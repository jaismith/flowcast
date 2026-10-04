import { Loader, Skeleton } from './Skeleton.jsx';
import { placeOf } from '../lib/search.js';
import { placeName } from './Editorial.jsx';

const link = 'underline decoration-line underline-offset-2 hover:decoration-ink';

/** The river page's frame in shimmer while a site resolves or its forecast files load, with one loader over the chart. */
export function SiteLoading({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site ? (
          <>
            {site.river} {site.town && <span className="font-normal text-muted">at {placeName(placeOf(site))}</span>}
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

/** A catalog gauge flowcast can't forecast, with the backend's reason. */
export function GaugeNotForecastable({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site.river} {site.town && <span className="font-normal text-muted">at {placeName(placeOf(site))}</span>}
      </h1>
      <div className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        <p className="font-medium text-ink">flowcast can’t forecast this gauge.</p>
        <p className="mt-1">
          {site.reason}. Search above or open the map to find a gauge nearby.{' '}
          <a className={link} href={`https://waterdata.usgs.gov/monitoring-location/${site.id}/`} target="_blank" rel="noreferrer">
            USGS gauge page
          </a>
        </p>
      </div>
    </Shell>
  );
}

/** A USGS id that isn't a flowcast site (and isn't in a catalog tile the map has loaded). */
export function GaugeUnsupported({ id }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">flowcast doesn’t forecast USGS {id.replace('USGS-', '')}</h1>
      <p className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        It isn’t one of the basins flowcast covers. Search above by river, town or USGS number, or open the map to see which gauges nearby can be
        forecast.{' '}
        <a className={link} href={`https://waterdata.usgs.gov/monitoring-location/${id}/`} target="_blank" rel="noreferrer">
          USGS gauge page
        </a>
      </p>
    </Shell>
  );
}

/** A route that isn't a USGS id or a site slug. */
export function SiteNotFound({ id }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">No gauge called “{id}”</h1>
      <p className="mt-6 max-w-2xl border-t border-line pt-6 text-base text-muted">
        That isn’t a USGS gauge number or a flowcast site. Search above by river, town or USGS number, or open the map to browse every gauge.
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
