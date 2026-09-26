from __future__ import annotations

from collections.abc import Callable, Iterator

from ..context import Context
from ..model import Issuance
from . import hefs, iem, legacy, nwps, rating, usgs_temp

Source = Callable[[Context], Iterator[Issuance]]

# Ratings run before RVF bulletins, which reuse the fetched ratings for stage-to-flow.
SOURCES: dict[str, Source] = {
    "marfc_nwps": nwps.collect_marfc,
    "nwm": nwps.collect_nwm,
    "hefs": hefs.collect,
    "usgs_rating": rating.collect,
    "marfc_rvf": iem.collect,
    "usgs_drb_temp": usgs_temp.collect,
    "flowcast_legacy": legacy.collect,
}
