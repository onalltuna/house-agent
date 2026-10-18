"""Fetch and parse current Plaza listings — the same API the "Wonen" page on the site uses.

Note: the older /portal/object/frontend/getallobjects endpoint also returns stale listings
(e.g. old "extra aanbod" batches), so don't use it to decide what's available.
"""
import httpx

from house_agent.models import Listing
from house_agent.plaza.session import BASE_URL, USER_AGENT

API_URL = "https://mosaic-plaza-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
DETAIL_URL = f"{BASE_URL}/aanbod/huurwoningen/details/"
PAGE_SIZE = 60

# Copied from the request the site's /aanbod/wonen page sends: rental housing, no
# "extra aanbod" or house swaps.
WONEN_FILTER = {
    "hidden-filters": {
        "$and": [
            {"dwellingType.categorie": {"$eq": "woning"}},
            {"rentBuy": {"$eq": "Huur"}},
            {"isExtraAanbod": {"$eq": ""}},
            {"isWoningruil": {"$eq": ""}},
        ]
    }
}

# Plaza's region names start with the Dutch country name, e.g. "Duitsland - Noordrijn-Westfalen".
COUNTRY_NAMES = {"Nederland": "Netherlands", "Duitsland": "Germany", "België": "Belgium"}
# Allocation models where the earliest reaction wins.
FCFS_MODEL_CODES = {"reactiedatum", "dth"}


def parse(raw: dict) -> Listing:
    dutch_country, _, province = (p.strip() for p in raw["regio"]["name"].partition(" - "))
    address = " ".join(
        str(p) for p in (raw["street"], raw["houseNumber"], raw["houseNumberAddition"]) if p
    )
    model = raw.get("model") or {}
    category = (raw.get("toewijzingModelCategorie") or {}).get("code") or ""
    return Listing(
        site="plaza",
        id=str(raw["id"]),
        url=DETAIL_URL + raw["urlKey"],
        address=address,
        city=raw["city"]["name"],
        country=COUNTRY_NAMES.get(dutch_country, dutch_country),
        dwelling_type=raw["dwellingType"]["localizedName"] or "",
        total_rent=float(raw.get("totalRent") or 0),
        area_m2=int(raw.get("areaDwelling") or 0),
        first_come_first_serve=bool(model.get("advertentieSluitenNaEersteReactie"))
        or model.get("code") in FCFS_MODEL_CODES
        or category in FCFS_MODEL_CODES,
        target_groups=tuple(g["code"] for g in raw.get("doelgroepen") or [] if g.get("code")),
        published=(raw.get("publicationDate") or "")[:16].replace("T", " "),
        available_from=(raw.get("availableFromDate") or "")[:10],
        reactions=raw.get("numberOfReactions"),
        region=province,
    )


def fetch_raw(client: httpx.Client) -> list[dict]:
    results, page = [], 0
    while True:
        resp = client.post(
            API_URL,
            params={"limit": PAGE_SIZE, "locale": "nl_NL", "page": page, "sort": "-publicationDate"},
            json=WONEN_FILTER,
        )
        resp.raise_for_status()
        body = resp.json()
        results += body["data"]
        page += 1
        if page >= body["_metadata"]["page_count"]:
            return results


def fetch_listings(filters=None, client: httpx.Client | None = None) -> list[Listing]:
    """`filters` is unused: Plaza returns everything in one call and we filter afterwards."""
    own_client = client is None
    client = client or httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{BASE_URL}/aanbod/wonen",
        },
        timeout=20,
    )
    try:
        return [parse(r) for r in fetch_raw(client)]
    finally:
        if own_client:
            client.close()
