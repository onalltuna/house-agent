"""React ("Reageren") to a Plaza listing with the logged-in session.

The request mirrors what the site's own "Reageer" button sends (see docs/recon.md):
    POST /portal/object/frontend/react/format/json   (form-encoded)
    __id__, __hash__  ← the "submit only" form configuration
    add, dwellingID   ← query string in reagerenData.url (only given to logged-in users)

    uv run python -m house_agent.plaza.apply 17115            # show status + payload, sends nothing
    uv run python -m house_agent.plaza.apply 17115 --really   # actually react
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

import httpx

from house_agent.plaza.listings import DETAIL_URL

STATUS_PATH = "/portal/object/frontend/getreagerendata/format/json"
FORM_PATH = "/portal/core/frontend/getformsubmitonlyconfiguration/format/json"
REACT_PATH = "/portal/object/frontend/react/format/json"
XHR = {"X-Requested-With": "XMLHttpRequest"}


class ApplyError(RuntimeError):
    """A reason not to (or failure to) react, written for the user."""


@dataclass
class ReactResult:
    ok: bool
    message: str
    position: int | None
    seconds: float


def reaction_status(client: httpx.Client, listing_id: str) -> dict:
    """Plaza's view of this listing for the logged-in user: kanReageren, action, url, voorlopigePositie…"""
    r = client.post(STATUS_PATH, data={"objectId[]": listing_id}, headers=XHR)
    r.raise_for_status()
    return (r.json().get("reagerenData") or {}).get(str(listing_id)) or {}


def check_can_react(status: dict) -> None:
    if not status:
        raise ApplyError("Plaza returned no reaction data — the listing is probably gone.")
    if status.get("action") != "add":
        raise ApplyError("You've already reacted to this listing.")
    if not status.get("kanReageren"):
        raise ApplyError("Plaza says you can't react to this listing (not eligible, or reactions closed).")
    if status.get("moetMotiveren"):
        raise ApplyError("This listing requires a motivation text — please react on the website.")
    if not status.get("url"):
        raise ApplyError("Plaza didn't provide a reaction link for this listing.")


def build_payload(client: httpx.Client, status: dict) -> dict:
    form = client.get(FORM_PATH, headers=XHR).json()["form"]
    payload = {"__id__": form["id"]}
    for name, element in form["elements"].items():
        if isinstance(element, dict) and "initialData" in element:
            payload[name] = element["initialData"]
    payload.update(parse_qsl(status["url"].lstrip("?")))
    return payload


def _plain_text(markup: str) -> str:
    """Plaza's confirmation messages are HTML; keep only the readable text."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return re.sub(r"\s+", " ", text).strip()


def _messages(body: dict) -> str:
    msgs = body.get("messages") or []
    if isinstance(msgs, dict):
        msgs = list(msgs.values())
    texts = [m if isinstance(m, str) else str(m.get("message") or m.get("text") or m) for m in msgs]
    return _plain_text(" ".join(texts) or str(body.get("sMessage") or ""))


def react(client: httpx.Client, listing_id: str) -> ReactResult:
    """React to one listing. Raises ApplyError if Plaza says we can't."""
    started = time.monotonic()
    status = reaction_status(client, listing_id)
    check_can_react(status)
    payload = build_payload(client, status)
    resp = client.post(
        REACT_PATH, data=payload, headers={**XHR, "Referer": DETAIL_URL + str(listing_id)}
    )
    resp.raise_for_status()
    body = resp.json()
    seconds = time.monotonic() - started

    # Don't trust the response shape alone: ask Plaza again whether we're now listed as reacted.
    after = reaction_status(client, listing_id)
    ok = after.get("action") not in (None, "add") or body.get("success") is True
    return ReactResult(ok=ok, message=_messages(body), position=after.get("voorlopigePositie"), seconds=seconds)


def main() -> None:
    from house_agent.plaza.session import LoginError, ensure_logged_in

    parser = argparse.ArgumentParser()
    parser.add_argument("listing_id")
    parser.add_argument("--really", action="store_true", help="actually send the reaction")
    args = parser.parse_args()
    try:
        client = ensure_logged_in()
    except LoginError as e:
        sys.exit(f"❌ {e}")
    try:
        status = reaction_status(client, args.listing_id)
        print("Plaza status:", {k: status.get(k) for k in ("kanReageren", "action", "voorlopigePositie", "url")})
        if not args.really:
            check_can_react(status)
            payload = build_payload(client, status)
            print("Would send:", {k: ("<token>" if k == "__hash__" else v) for k, v in payload.items()})
            print("Dry run — nothing sent. Add --really to react.")
            return
        result = react(client, args.listing_id)
        print(("✅" if result.ok else "⚠️"), result)
    except ApplyError as e:
        sys.exit(f"❌ {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
