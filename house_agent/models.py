from dataclasses import dataclass


@dataclass(frozen=True)
class Listing:
    site: str
    id: str
    url: str
    address: str
    city: str
    country: str
    dwelling_type: str
    total_rent: float
    area_m2: int
    first_come_first_serve: bool
    target_groups: tuple[str, ...]
    published: str
    available_from: str
    reactions: int | None = None
    region: str = ""  # province (Plaza) or the site's own city/area name (Fizz)

    @property
    def key(self) -> str:
        return f"{self.site}:{self.id}"
