"""Dams covered by the prototype sources, with NID ids and coordinates (NID, data as of 2026-09-23).

SafeWaters facilities carry their own coordinates in the page data, so they aren't listed here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dam:
    name: str
    nid_id: str
    lat: float
    lon: float
    tz: str
    # USACE CWMS observed hourly outflow (office, time-series id): the dam's own measured release.
    cwms: tuple[str, str] | None = None


_C = "America/Chicago"


def _swt(code: str) -> tuple[str, str]:
    return ("SWT", f"{code}.Flow-Res Out.Ave.1Hour.1Hour.Rev-Regi-Flowgroup")


def _swl(loc: str) -> tuple[str, str]:
    return ("SWL", f"{loc}.Flow-Res Out.Ave.1Hour.1Hour.Regi-Comp")


SWPA = {
    "Broken Bow": Dam("Broken Bow", "OK10307", 34.14616, -94.68536, _C, _swt("BROK")),
    "Denison": Dam("Denison", "OK10317", 33.83022, -96.57087, _C, _swt("DENI")),
    "Keystone": Dam("Keystone", "OK10309", 36.14989, -96.25297, _C, _swt("KEYS")),
    "Fort Gibson": Dam("Fort Gibson", "OK10314", 35.86965, -95.23083, _C, _swt("FGIB")),
    "Webbers Falls L&D": Dam("Webbers Falls L&D", "OK10304", 35.55397, -95.16871, _C, _swt("WEBB")),
    "Tenkiller": Dam("Tenkiller", "OK10311", 35.59374, -95.03794, _C, _swt("TENK")),
    "Eufaula": Dam("Eufaula", "OK10308", 35.30450, -95.36025, _C, _swt("EUFA")),
    "Robert S. Kerr L&D": Dam("Robert S. Kerr L&D", "OK10301", 35.34679, -94.77740, _C),
    "Ozark L&D": Dam("Ozark L&D", "AR00164", 35.47196, -93.81413, _C),
    "Dardanelle L&D": Dam("Dardanelle L&D", "AR00162", 35.24958, -93.17005, _C),
    "Beaver": Dam("Beaver", "AR00174", 36.42221, -93.84762, _C, _swl("Beaver_Dam")),
    "Table Rock": Dam("Table Rock", "MO30202", 36.59597, -93.31081, _C, _swl("Table_Rock_Dam")),
    "Bull Shoals": Dam("Bull Shoals", "AR00160", 36.36599, -92.57523, _C, _swl("Bull_Shoals_Dam")),
    "Norfork": Dam("Norfork", "AR00159", 36.24957, -92.23826, _C, _swl("Norfork_Dam")),
    "Greers Ferry": Dam("Greers Ferry", "AR00173", 35.52118, -91.99423, _C, _swl("GreersFerry_Dam")),
    "Stockton": Dam("Stockton", "MO30200", 37.69190, -93.75950, _C),
    "Harry S Truman": Dam("Harry S Truman", "MO20725", 38.26375, -93.40357, _C),
    "Clarence Cannon": Dam("Clarence Cannon", "MO82201", 39.52441, -91.64385, _C),
}

LCRA = {
    "Buchanan": Dam("Buchanan", "TX00989", 30.75135, -98.41791, _C),
    "Inks": Dam("Inks", "TX00988", 30.73075, -98.38439, _C),
    "Wirtz": Dam("Wirtz", "TX00986", 30.55546, -98.33807, _C),
    "Starcke": Dam("Starcke", "TX00987", 30.55648, -98.25659, _C),
    "Mansfield": Dam("Mansfield", "TX01087", 30.39222, -97.90734, _C),
    "Tom Miller": Dam("Tom Miller", "TX01086", 30.29406, -97.78641, _C),
}
