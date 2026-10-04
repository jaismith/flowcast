import { canonicalId, placeOf } from './sites.js';

const link = 'underline decoration-line underline-offset-2 hover:decoration-ink';

/** The river page's title block while a site resolves or its forecast files load, so the header doesn't jump. */
export function SiteLoading({ site }) {
  return (
    <Shell>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {site ? (
          <>
            {site.river} {site.town && <span className="font-normal text-muted">at {placeOf(site)}</span>}
          </>
        ) : (
          <span className="text-faint">Finding the gauge…</span>
        )}
      </h1>
      <div className="mt-6 border-t border-line pt-6 text-[13px] text-muted">{site ? 'Loading the forecast…' : 'Asking USGS about this gauge…'}</div>
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
