"""Display names from USGS station names: "DELAWARE RIVER AT CALLICOON NY" -> river "Delaware River",
town "Callicoon", state "NY", name "Delaware River at Callicoon, NY".

The state is the town's (the one in the station name), not the gauge record's: the Callicoon and Lordville gauges
are filed under Pennsylvania (Wayne County bank) while both towns are in New York.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}
CODES = set(STATES.values())
# Station-name abbreviations (USGS naming conventions)
WORDS = {
    "R": "River", "RIV": "River", "CR": "Creek", "CK": "Creek", "C": "Creek", "BR": "Branch", "BK": "Brook", "FK": "Fork", "F": "Fork", "TRIB": "Tributary",
    "E": "East", "W": "West", "N": "North", "S": "South", "NE": "Northeast", "NW": "Northwest", "SE": "Southeast", "SW": "Southwest",
    "MID": "Middle", "MIDDLE": "Middle", "LT": "Little", "LTL": "Little", "LIT": "Little", "MT": "Mount", "MTN": "Mountain", "ST": "St.",
    "STE": "Ste.", "FT": "Fort", "PT": "Point", "SPGS": "Springs", "SPG": "Spring", "HTS": "Heights", "JCT": "Junction", "LK": "Lake",
    "RES": "Reservoir", "STA": "Station", "VLY": "Valley", "PK": "Park", "BRDG": "Bridge", "CTR": "Center", "TWP": "Township",
}
RELATIONS = {"AT": "at", "NEAR": "near", "NR": "near", "ABOVE": "above", "AB": "above", "BELOW": "below", "BL": "below", "BLW": "below", "ABV": "above"}
SMALL = {"of", "and", "the", "at", "near", "above", "below", "de", "du", "la"}


@dataclass(frozen=True)
class StationName:
    name: str
    river: str
    town: str
    state: str


def _words(text: str) -> str:
    out = []
    for i, raw in enumerate(re.split(r"\s+", text.strip())):
        token = raw.strip(".,")
        up = token.upper()
        if up in WORDS and (token.isupper() or len(up) <= 2):
            word = WORDS[up]
        else:
            word = token.lower() if (token.lower() in SMALL and i > 0) else token.capitalize() if token.isupper() or token.islower() else token
            word = re.sub(r"(?<=[-'])([a-z])", lambda m: m.group(1).upper(), word)
            word = re.sub(r"^Mc([a-z])", lambda m: "Mc" + m.group(1).upper(), word)
        out.append(word)
    return " ".join(out)


ABBREVIATED_STATES = {"w va": "WV", "w virginia": "WV", "n h": "NH", "n y": "NY", "n j": "NJ", "n c": "NC", "s c": "SC", "r i": "RI", "d c": "DC", "mass": "MA", "penn": "PA", "conn": "CT"}


def _split_state(rest: str, fallback: str) -> tuple[str, str]:
    tokens = [t for t in re.split(r"[,\s]+", rest.strip(" .,")) if t]
    for size in (3, 2, 1):
        if len(tokens) <= size:
            continue
        tail = " ".join(t.strip(".") for t in tokens[-size:]).lower()
        code = STATES.get(tail) or ABBREVIATED_STATES.get(tail) or (tail.upper() if size == 1 and tail.upper() in CODES else None)
        if code:
            return " ".join(tokens[:-size]), code
    return " ".join(tokens), fallback


def parse(station_name: str, fallback_state: str = "") -> StationName:
    text = re.sub(r"\s*\([^)]*\)|\s*-\s*\d{8,15}$", "", station_name)
    text = re.sub(r"\s+", " ", text.strip())
    m = re.search(r"\s(" + "|".join(sorted(RELATIONS, key=len, reverse=True)) + r")\.?\s", text, flags=re.IGNORECASE)
    if m is None:
        rest, state = _split_state(text, fallback_state)
        river = _words(rest)
        return StationName(f"{river}, {state}" if state else river, river, "", state)
    river = _words(text[: m.start()])
    rel = RELATIONS[m.group(1).upper()]
    place_raw, state = _split_state(text[m.end():], fallback_state)
    place = _words(place_raw)
    # "Mill River at Spring Street at Taunton": the town is the last place named
    town = re.split(r"\s(?:at|near|above|below)\s", place)[-1]
    name = f"{river} {rel} {place}" + (f", {state}" if state else "")
    return StationName(name, river, town, state)
