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
CONFLICT = 409  # another program is polling the same bot


class TelegramError(httpx.HTTPError):
    """A failed Bot API call. The message never contains the URL, because the URL contains the bot token."""

    def __init__(self, method: str, status: int | None, description: str):
        super().__init__(f"Telegram {method} failed ({status}): {description}")
        self.status = status


class Telegram:
    def __init__(self, token: str, chat_id: str | None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=40)
        self.update_offset = 0

    def _call(self, method: str, json: dict | None = None, params: dict | None = None) -> dict:
        try:
            r = self.http.post(f"{self.api}/{method}", json=json, params=params)
        except httpx.HTTPError as e:
            raise TelegramError(method, None, type(e).__name__) from None
        if r.status_code != 200:
            try:
                description = r.json().get("description", "")
            except ValueError:
                description = r.text[:200]
            raise TelegramError(method, r.status_code, description)
        return r.json()

    @classmethod
    def from_env(cls) -> "Telegram":
        load_dotenv(".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("❌ Set TELEGRAM_BOT_TOKEN in .env")
        return cls(token, os.getenv("TELEGRAM_CHAT_ID"))

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None,
             reply_to: int | None = None) -> None:
        """Send HTML text, split over several messages if it's too long.

        `buttons` are (label, callback_data) pairs shown under the last message, one per row.
        """
        chunks = _chunks(text)
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]}
            if reply_to and i == 0:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            self._call("sendMessage", json=payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Stop the button's loading spinner (and optionally show a short toast)."""
        try:
            self._call("answerCallbackQuery", json={"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass  # cosmetic only

    def remove_button(self, message_id: int, keyboard: list[list[dict]], data: str) -> None:
        """Remove one button (by its callback data) from a message, keeping the others."""
        rows = [[b for b in row if b.get("callback_data") != data] for row in keyboard]
        try:
            self._call("editMessageReplyMarkup",
                       json={"chat_id": self.chat_id, "message_id": message_id,
                             "reply_markup": {"inline_keyboard": [r for r in rows if r]}})
        except TelegramError:
            pass  # e.g. message too old to edit; the duplicate check still prevents double applying

    def get_updates(self, timeout: int = 0) -> list[dict]:
        updates = self._call("getUpdates", params={"offset": self.update_offset, "timeout": timeout})["result"]
        if updates:
            self.update_offset = updates[-1]["update_id"] + 1
        return updates

    def events(self, timeout: int = 0) -> list[dict]:
        """Commands and button taps from our chat.

        With timeout > 0 this long-polls: it returns as soon as something arrives, so a tap on
        a button is handled within a fraction of a second.
        Returns dicts: {"type": "command", "command": "/list"} or
        {"type": "button", "data": "...", "callback_id": "...", "message_id": 123, "keyboard": [...]}.
        """
        events = []
        for u in self.get_updates(timeout):
            if (msg := u.get("message")) and str(msg["chat"]["id"]) == str(self.chat_id):
                if msg.get("text", "").startswith("/"):
                    events.append({"type": "command", "command": msg["text"].split()[0].split("@")[0].lower()})
            elif (cb := u.get("callback_query")) and str(cb["message"]["chat"]["id"]) == str(self.chat_id):
                events.append({"type": "button", "data": cb.get("data", ""), "callback_id": cb["id"],
                               "message_id": cb["message"]["message_id"],
                               "keyboard": (cb["message"].get("reply_markup") or {}).get("inline_keyboard", [])})
        return events


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
