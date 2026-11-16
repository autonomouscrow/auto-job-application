"""Rough region detection from free-text locations.

Good enough to decide whether a job is in a target region. When a location
cannot be placed it is reported as unknown, and unknown never filters a job.
"""

from __future__ import annotations

import re

from jobtool.normalize import fold

NORTH_AMERICA = "north-america"
EUROPE = "europe"
OTHER = "other"

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
CA_PROVINCES = set("ON QC BC AB MB SK NS NB NL PE YT NT NU".split())

_NA_WORDS = {
    "usa", "us", "united states", "united states of america", "america", "canada", "mexico",
    "nyc", "new york", "new york city", "sf", "san francisco", "la", "los angeles", "chicago",
    "boston", "seattle", "austin", "miami", "toronto", "vancouver", "montreal", "waterloo",
    "ottawa", "calgary", "bay area", "silicon valley", "houston", "dallas", "denver", "atlanta",
    "philadelphia", "greenwich", "stamford", "oakland", "princeton", "bala cynwyd",
}  # fmt: skip
_EUROPE_WORDS = {
    "europe", "eu", "emea", "uk", "united kingdom", "england", "scotland", "wales", "ireland",
    "northern ireland", "france", "germany", "netherlands", "the netherlands", "belgium",
    "luxembourg", "switzerland", "austria", "spain", "portugal", "italy", "greece", "malta",
    "cyprus", "denmark", "sweden", "norway", "finland", "iceland", "poland", "czechia",
    "czech republic", "slovakia", "hungary", "romania", "bulgaria", "croatia", "slovenia",
    "serbia", "estonia", "latvia", "lithuania", "ukraine", "turkey", "london", "dublin",
    "paris", "berlin", "munich", "frankfurt", "amsterdam", "zurich", "geneva", "madrid",
    "barcelona", "lisbon", "milan", "rome", "stockholm", "copenhagen", "oslo", "helsinki",
    "warsaw", "prague", "vienna austria", "budapest", "bucharest", "brussels", "edinburgh",
    "cambridge uk", "manchester", "zug",
}  # fmt: skip
_OTHER_WORDS = {
    "india", "china", "japan", "singapore", "hong kong", "taiwan", "south korea", "korea",
    "australia", "new zealand", "brazil", "argentina", "chile", "colombia", "israel", "uae",
    "united arab emirates", "saudi arabia", "qatar", "south africa", "nigeria", "kenya",
    "egypt", "vietnam", "thailand", "malaysia", "indonesia", "philippines", "pakistan",
    "bangladesh", "sydney", "melbourne", "tokyo", "shanghai", "beijing", "shenzhen",
    "bangalore", "bengaluru", "hyderabad", "mumbai", "pune", "delhi", "gurgaon", "chennai",
    "dubai", "tel aviv", "seoul", "sao paulo", "asia", "apac", "latam",
}  # fmt: skip


_NA_WORDS |= {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "ontario", "quebec", "british columbia", "alberta", "south sf", "washington dc",
}  # fmt: skip


def region_of(location: str) -> str | None:
    """Return the region of one location string, or None when it cannot be placed."""
    parts = [part.strip() for part in re.split(r"[,/;|()]| - ", location) if part.strip()]
    # The last part is usually the most specific about the country.
    for part in reversed(parts):
        folded = re.sub(r"^(remote|hybrid|onsite|on site)( in| within)? ", "", fold(part))
        if folded in _EUROPE_WORDS:
            return EUROPE
        if folded in _NA_WORDS:
            return NORTH_AMERICA
        if folded in _OTHER_WORDS:
            return OTHER
        code = part.strip().upper()
        if part.strip() == code and (code in US_STATES or code in CA_PROVINCES):
            return NORTH_AMERICA
    return None


def regions_of(locations: list[str]) -> set[str | None]:
    return {region_of(location) for location in locations}


def in_target_regions(locations: list[str], targets: list[str]) -> bool | None:
    """True or False when every location could be placed, otherwise None (unclear)."""
    if not locations or not targets:
        return None
    found = regions_of(locations)
    if found & set(targets):
        return True
    if None in found:
        return None
    return False
