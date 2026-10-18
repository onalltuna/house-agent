"""Fetch THE FIZZ apartments from their booking backend (no login needed).

Two steps, so a quiet check costs a single request:
1. `progressiveSearch/form` with an empty body lists every area (city) that has anything
   bookable, with counts. A fully booked city simply isn't in the list.
2. Only for areas matching our city filter: `progressiveSearch/normalized-search` returns
   the actual rooms and their offers (price, start/end date, booking type).
"""
import re

import httpx

from house_agent.filters import Filters
from house_agent.models import Listing

API = "https://booking.the-fizz.com/json-interface/rs/progressiveSearch/"
SEARCH_PAGE = "https://www.the-fizz.com/en/search/?searchcriteria=BUILDING:{building};AREA:{area};"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.the-fizz.com",
    "Referer": "https://www.the-fizz.com/",
}
# Renewals are for existing tenants and parking isn't housing.
EXCLUDED_BOOKING_TYPES = ("RENEWAL", "PARKING")
# The same booking backend also serves the sister brand HAVENS LIVING.
OTHER_BRAND_PREFIXES = ("HAVENS",)
NL_AREAS = {"UTRECHT", "LEIDEN"}
M2_PATTERN = re.compile(r"(\d+)\s*(?:m<sup>2|m²|qm)", re.I)


def _country(area_code: str) -> str:
    if area_code in NL_AREAS:
        return "Netherlands"
    if area_code.startswith("PRAG"):
        return "Czechia"
    if area_code.startswith(("WIEN", "VIENNA")):
        return "Austria"
    return "Germany"


def _area_tags(form: dict) -> list[dict]:
    for cat in form.get("categories", []):
        if cat["lookupValue"] == "AREA":
            return cat.get("tags", [])
    return []


def _wanted(area: dict, filters: Filters) -> bool:
    if filters.countries and _country(area["lookupValue"]).lower() not in {c.lower() for c in filters.countries}:
        return False
    if not filters.cities:
        return True
    name = area["displayValue"].lower()
    return any(city.lower() in name for city in filters.cities)


def _area_m2(room: dict) -> int:
    for detail in room.get("details", []):
        if m := M2_PATTERN.search(detail.get("value") or ""):
            return int(m.group(1))
    return 0


def _monthly(price: dict) -> float:
    if "month" in (price.get("unit") or ""):
        return float(price["number"])
    return round(float(price.get("dayRate") or 0) * 30, 2)


def parse_search(data: dict) -> list[Listing]:
    """One Listing per room, using its cheapest bookable offer."""
    booking_types = {b["id"]: b["displayValue"] for b in data["bookingTypes"].values()}
    room_types = {t["id"]: t["displayValue"] for t in data["roomTypes"].values()}
    buildings = {b["id"]: b["lookupValue"] for b in data["buildings"].values()}
    areas = {a["id"]: a for a in data["areas"].values()}

    offers_by_room: dict[str, list[dict]] = {}
    for offer in data["offers"]:
        if offer.get("bookable"):
            offers_by_room.setdefault(offer["roomId"], []).append(offer)

    listings = []
    for room_id, offers in offers_by_room.items():
        room = data["rooms"][room_id]
        if buildings.get(room["buildingId"], "").startswith(OTHER_BRAND_PREFIXES):
            continue
        cheapest = min(offers, key=lambda o: _monthly(o["price"]))
        area = areas[room["areaId"]]
        addr = room["address"]
        stay_types = sorted({booking_types.get(o["bookingTypeId"], "?").strip() for o in offers})
        listings.append(
            Listing(
                site="fizz",
                id=room["lookupValue"],
                url=SEARCH_PAGE.format(building=buildings.get(room["buildingId"], ""), area=area["lookupValue"]),
                address=f'{addr.get("line2", "")} ({room["displayValue"]})'.strip(),
                city=addr.get("line3") or area["displayValue"],
                country=_country(area["lookupValue"]),
                dwelling_type=f'{room_types.get(room["roomTypeId"], "Apartment")} · {", ".join(stay_types)}',
                total_rent=_monthly(cheapest["price"]),
                area_m2=_area_m2(room),
                first_come_first_serve=True,  # booked online, first to complete it gets it
                target_groups=(),
                published="",
                available_from=min(o["start"] for o in offers),
                region=area["displayValue"],
            )
        )
    return listings


def fetch_listings(filters: Filters, client: httpx.Client | None = None) -> list[Listing]:
    own_client = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=30)
    try:
        resp = client.post(API + "form", json={})
        resp.raise_for_status()
        form = resp.json()
        areas = [a for a in _area_tags(form) if _wanted(a, filters)]
        booking_types = [
            {"lookupValue": b["lookupValue"]}
            for b in form.get("bookingTypes", [])
            if not b["lookupValue"].startswith(EXCLUDED_BOOKING_TYPES)
        ]
        if not areas or not booking_types:
            return []
        resp = client.post(
            API + "normalized-search",
            json={"categories": [{"lookupValue": "AREA", "tags": areas}], "bookingTypes": booking_types},
        )
        resp.raise_for_status()
        data = resp.json()
        return parse_search(data) if data.get("offers") else []
    finally:
        if own_client:
            client.close()
