"""USGS parameter and statistic codes used by flowcast."""

from enum import StrEnum


class Parameter(StrEnum):
    DISCHARGE = "00060"
    GAGE_HEIGHT = "00065"
    WATER_TEMPERATURE = "00010"


class Statistic(StrEnum):
    MAXIMUM = "00001"
    MINIMUM = "00002"
    MEAN = "00003"
    INSTANTANEOUS = "00011"


UNITS: dict[Parameter, str] = {
    Parameter.DISCHARGE: "ft3/s",
    Parameter.GAGE_HEIGHT: "ft",
    Parameter.WATER_TEMPERATURE: "degC",
}


def site_id(site: str) -> str:
    """Normalize a gauge number to the API's `AGENCY-NUMBER` form ("01427510" -> "USGS-01427510")."""
    site = site.strip()
    return site if "-" in site else f"USGS-{site}"


def site_number(site: str) -> str:
    return site_id(site).split("-", 1)[1]
