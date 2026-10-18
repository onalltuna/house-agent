"""Phase 0 probe: fetch Plaza listings once and print a summary.

Usage: python3 scripts/plaza_probe.py [--city Utrecht] [--save tests/fixtures/plaza_listings.json]
Only uses the Python standard library.
"""
import argparse
import json
import urllib.request

LISTINGS_URL = "https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json"


def fetch_listings() -> dict:
    req = urllib.request.Request(
        LISTINGS_URL,
        data=b"",
        method="POST",
        headers={"Accept": "application/json", "User-Agent": "house-agent/0.1 (personal use)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="Utrecht")
    parser.add_argument("--save", help="write raw JSON to this path (for test fixtures)")
    args = parser.parse_args()

    data = fetch_listings()
    listings = data["result"]
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    matches = [l for l in listings if l["city"]["name"].lower() == args.city.lower()]
    print(f"{len(listings)} listings total, {len(matches)} in {args.city}\n")
    for l in matches:
        fcfs = l["model"]["advertentieSluitenNaEersteReactie"]
        print(
            f'{l["id"]:>6}  {l["dwellingType"]["localizedName"]:<8} '
            f'€{l["totalRent"]:>8.2f}  {l["areaDwelling"]:>3} m²  '
            f'{"FCFS" if fcfs else (l["model"]["modelCategorie"]["code"] or "?"):<12} '
            f'pub {l["publicationDate"]}  '
            f'https://plaza.newnewnew.space/aanbod/huurwoningen/details/{l["urlKey"]}'
        )


if __name__ == "__main__":
    main()
