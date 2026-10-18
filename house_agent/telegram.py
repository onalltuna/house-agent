"""Minimal Telegram Bot API client.

    uv run python -m house_agent.telegram   # one-time: finds your chat id and saves it to .env
"""
import html
import os
import sys
import time

import httpx
from dotenv import load_dotenv, set_key

from house_agent.models import Listing

MAX_MESSAGE_LEN = 4000  # Telegram's hard limit is 4096


class Telegram:
    def __init__(self, token: str, chat_id: str | None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=40)
        self.update_offset = 0

    @classmethod
    def from_env(cls) -> "Telegram":
        load_dotenv(".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("❌ Set TELEGRAM_BOT_TOKEN in .env")
        return cls(token, os.getenv("TELEGRAM_CHAT_ID"))

    def send(self, text: str) -> None:
        """Send HTML text, split over several messages if it's too long."""
        for chunk in _chunks(text):
            r = self.http.post(
                f"{self.api}/sendMessage",
                json={"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            )
            r.raise_for_status()

    def get_updates(self, timeout: int = 0) -> list[dict]:
        r = self.http.get(f"{self.api}/getUpdates", params={"offset": self.update_offset, "timeout": timeout})
        r.raise_for_status()
        updates = r.json()["result"]
        if updates:
            self.update_offset = updates[-1]["update_id"] + 1
        return updates

    def commands(self) -> list[str]:
        """Return commands like '/list' sent by our chat since the last call."""
        return [
            u["message"]["text"].split()[0].split("@")[0].lower()
            for u in self.get_updates()
            if "message" in u
            and str(u["message"]["chat"]["id"]) == str(self.chat_id)
            and u["message"].get("text", "").startswith("/")
        ]


def _chunks(text: str) -> list[str]:
    """Split on blank lines so no listing is cut in half."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    return chunks + [current] if current else chunks


def format_listing(l: Listing) -> str:
    fcfs = "⚡ <b>First come, first served</b>\n" if l.first_come_first_serve else ""
    groups = f" · {', '.join(l.target_groups)}" if l.target_groups else ""
    reactions = f" · {l.reactions} reactions so far" if l.reactions is not None else ""
    size = f" · {l.area_m2} m²" if l.area_m2 else ""
    return (
        f"🏠 <b>{html.escape(l.address)}, {html.escape(l.city)}</b>\n"
        f"{fcfs}"
        f"{html.escape(l.dwelling_type)} · €{l.total_rent:.2f}/mo{size}{html.escape(groups)}\n"
        f"Available from {l.available_from or '?'}{reactions}\n"
        f'<a href="{html.escape(l.url)}">Open on {l.site.title()}</a>'
    )


def format_list(title: str, listings: list[Listing]) -> str:
    if not listings:
        return f"<b>{html.escape(title)}</b>\nNothing matches your filters right now."
    listings = sorted(listings, key=lambda l: (not l.first_come_first_serve, l.total_rent))
    return f"<b>{html.escape(title)} ({len(listings)})</b>\n\n" + "\n\n".join(format_listing(l) for l in listings)


def setup() -> None:
    """Wait for the user to message the bot, then store their chat id in .env."""
    tg = Telegram.from_env()
    print("Open Telegram, find your bot and send it any message (e.g. /start). Waiting…")
    deadline = time.time() + 300
    while time.time() < deadline:
        for u in tg.get_updates(timeout=30):
            if "message" in u:
                chat = u["message"]["chat"]
                set_key(".env", "TELEGRAM_CHAT_ID", str(chat["id"]))
                tg.chat_id = str(chat["id"])
                tg.send("✅ House agent connected. You'll get Plaza listings here.")
                print(f"✅ Saved TELEGRAM_CHAT_ID for {chat.get('first_name') or chat.get('title')}")
                return
    sys.exit("❌ No message received within 5 minutes.")


if __name__ == "__main__":
    setup()
