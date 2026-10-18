import tomllib
from dataclasses import dataclass
from pathlib import Path

from house_agent.models import Listing


@dataclass
class Filters:
    countries: list[str]
    cities: list[str]

    def matches(self, listing: Listing) -> bool:
        if self.countries and listing.country.lower() not in {c.lower() for c in self.countries}:
            return False
        # "cities" may also name a region/province, e.g. "Noord-Brabant".
        if self.cities and not {listing.city.lower(), listing.region.lower()} & {c.lower() for c in self.cities}:
            return False
        return True


def load_config(path: str | Path = "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_filters(config: dict) -> Filters:
    f = config.get("filters", {})
    return Filters(countries=f.get("countries", []), cities=f.get("cities", []))
