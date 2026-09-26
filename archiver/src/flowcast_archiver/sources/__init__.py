from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta

from ..context import Context
from ..model import Issuance
from . import cwms, hefs, iem, legacy, nwps, nyc_dep, odrm, rating, thermal, tva, usgs_temp

Collect = Callable[[Context], Iterator[Issuance]]
SIX_HOURS = timedelta(hours=6)


@dataclass(frozen=True)
class Source:
    collect: Collect
    every: timedelta | None = None  # None: every (hourly) run
    once: bool = False  # one-time pull; runs again only when forced


# Run order matters: ratings before RVF bulletins (which reuse the fetched ratings), and the
# hourly benchmark forecasts before the slower release-schedule sources, which skip a run
# when the time budget is short and catch up the next hour.
SOURCES: dict[str, Source] = {
    "marfc_nwps": Source(nwps.collect_marfc),
    "nwm": Source(nwps.collect_nwm),
    "hefs": Source(hefs.collect),
    "usgs_rating": Source(rating.collect),
    "marfc_rvf": Source(iem.collect),
    "usgs_drb_temp": Source(usgs_temp.collect),
    "flowcast_legacy": Source(legacy.collect),
    "odrm": Source(odrm.collect, every=SIX_HOURS),
    "nyc_dep_release": Source(nyc_dep.collect_release_page, every=SIX_HOURS),
    "nysdec_thermal": Source(thermal.collect, every=SIX_HOURS),
    "cwms_forecast": Source(cwms.collect, every=SIX_HOURS),
    "tva_predicted": Source(tva.collect, every=SIX_HOURS),
    "nyc_dep_opendata": Source(nyc_dep.collect_opendata, once=True),
}
