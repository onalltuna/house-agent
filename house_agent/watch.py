"""Watch housing sites and send matching listings to Telegram.

    uv run python -m house_agent.watch          # run forever
    uv run python -m house_agent.watch --once   # one check, then exit

On start it sends the full list of matching listings; after that only changes.
In Telegram, send /list any time for everything currently available (one message per site),
or /plaza, /fizz for a single site.
Each site is checked and backed off independently, so one site failing doesn't affect the others.
"""
import argparse
import html
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from house_agent.filters import Filters, load_config, load_filters
from house_agent.fizz import listings as fizz
from house_agent.models import Listing
from house_agent.plaza import apply as plaza_apply
from house_agent.plaza import listings as plaza
from house_agent.plaza.session import LoginError, ensure_logged_in, get_account
from house_agent.store import Store
from house_agent.telegram import CONFLICT, Telegram, TelegramError, format_list, format_listing

log = logging.getLogger("watch")
COMMAND_POLL_SECONDS = 5  # pause after a failed Telegram poll
LONG_POLL_SECONDS = 25  # Telegram holds the request open until a command/tap arrives (max this long)
BLOCKED_STATUSES = {403, 429, 503}  # rate limited or Cloudflare challenge
BACKOFF_START = 10 * 60  # first wait after being blocked; doubles each time
BACKOFF_MAX = 2 * 60 * 60
WARN_AFTER_FAILURES = 5  # other errors: warn once after this many in a row

FETCHERS: dict[str, Callable[[Filters], list[Listing]]] = {
    "plaza": plaza.fetch_listings,
    "fizz": fizz.fetch_listings,
}
SITE_NAMES = {"plaza": "Plaza", "fizz": "THE FIZZ"}


@dataclass
class Source:
    site: str
    fetch: Callable[[Filters], list[Listing]]
    latest: list[Listing] = field(default_factory=list)
    backoff: float = 0  # >0 while the site is blocking us
    paused_until: float = 0
    failures: int = 0

    @property
    def name(self) -> str:
        return SITE_NAMES.get(self.site, self.site)


APPLY_PREFIX = "apply:"


def can_apply(listing: Listing, config: dict) -> bool:
    return listing.site == "plaza" and config.get("apply", {}).get("enabled", False)


def check(source: Source, store: Store, tg: Telegram, filters: Filters, announce: bool, config: dict) -> None:
    listings = [l for l in source.fetch(filters) if filters.matches(l)]
    new, gone = store.sync(source.site, listings)
    source.latest = listings
    log.info("%s: %d matching listings, %d new, %d gone", source.name, len(listings), len(new), len(gone))
    if announce:
        if any(can_apply(l, config) for l in new):
            warm_up_login()
        for l in new:
            buttons = [("⚡ Apply now", APPLY_PREFIX + l.key)] if can_apply(l, config) else None
            tg.send("🆕 " + format_listing(l), buttons=buttons)
        for l in gone:
            tg.send(f"❌ No longer listed on {source.name}: {l.address}, {l.city}")


SESSION_FRESH_SECONDS = 5 * 60  # trust a session we verified this recently without re-checking
SESSION_KEEPALIVE_SECONDS = 10 * 60  # how often to verify the session in the background
LOGIN_RETRY_SECONDS = 30 * 60  # after a failed login, wait this long before trying again


class PlazaSession:
    """One logged-in Plaza client kept open, so a tap doesn't pay for a login or new connections.

    Failed logins are rate limited: repeated failed logins are what gets accounts locked.
    """

    def __init__(self):
        self.client: httpx.Client | None = None
        self.verified_at = 0.0
        self.login_failed_at = 0.0
        self.on_login_failure: Callable[[str], None] | None = None

    def get(self, max_age: float = SESSION_FRESH_SECONDS) -> httpx.Client:
        if self.client and time.time() - self.verified_at < max_age:
            return self.client
        if self.client:
            try:
                if get_account(self.client):
                    self.verified_at = time.time()
                    return self.client
            except (httpx.HTTPError, ValueError):
                pass
            self.client.close()
            self.client = None
        wait = self.login_failed_at + LOGIN_RETRY_SECONDS - time.time()
        if wait > 0:
            raise LoginError(f"Plaza login failed recently; not retrying for another {wait / 60:.0f} min.")
        log.info("Logging in to Plaza…")
        try:
            self.client = ensure_logged_in()  # browser login only if the saved cookies stopped working
        except Exception as e:
            self.login_failed_at = time.time()
            if self.on_login_failure:
                self.on_login_failure(str(e))
            raise
        self.login_failed_at = 0.0
        self.verified_at = time.time()
        return self.client


plaza_session = PlazaSession()


def warm_up_login(max_age: float = 0) -> None:
    """Make sure the Plaza session is valid *before* you tap Apply, so the tap is instant."""
    try:
        plaza_session.get(max_age)
    except LoginError as e:
        log.warning("%s", e)
    except Exception:
        log.exception("Could not refresh the Plaza login")


def handle_apply(tg: Telegram, store: Store, config: dict, key: str, message_id: int,
                 keyboard: list[list[dict]] | None = None) -> None:
    """You tapped "Apply now": react on Plaza, with safety checks."""
    listing = store.listing(key)
    label = html.escape(f"{listing.address}, {listing.city}" if listing else key)
    site, _, listing_id = key.partition(":")
    max_per_day = config.get("apply", {}).get("max_per_day", 3)

    if site != "plaza" or not config.get("apply", {}).get("enabled", False):
        tg.send(f"❌ Applying isn't enabled for {label}.", reply_to=message_id)
        return
    if store.has_applied(key):
        tg.send(f"ℹ️ You already applied to {label}.", reply_to=message_id)
        return
    if store.applications_today() >= max_per_day:
        tg.send(f"🛑 Daily limit of {max_per_day} applications reached — not applying to {label}. "
                "Use the link to apply on the website, or raise max_per_day in config.toml.", reply_to=message_id)
        return

    tapped = time.monotonic()
    tg.remove_button(message_id, keyboard or [], APPLY_PREFIX + key)  # no double taps
    try:
        client = plaza_session.get()
        session_ready = time.monotonic() - tapped
        result = plaza_apply.react(client, listing_id)
        log.info("Apply %s: ok=%s, session ready after %.1fs, reaction sent after %.1fs (total %.1fs)",
                 key, result.ok, session_ready, result.seconds, time.monotonic() - tapped)
    except plaza_apply.ApplyError as e:
        log.info("Apply %s refused: %s", key, e)
        store.record_application(key, False, str(e))
        tg.send(f"❌ Not applied to {label}: {html.escape(str(e))}", reply_to=message_id)
        return
    except Exception as e:
        log.exception("Apply failed for %s", key)
        store.record_application(key, False, repr(e))
        tg.send(f"❌ Applying to {label} failed ({type(e).__name__}). Apply on the website now: "
                f'<a href="{html.escape(listing.url) if listing else ""}">open listing</a>', reply_to=message_id)
        return

    store.record_application(key, result.ok, result.message)
    position = f"\nYour provisional position: <b>{result.position}</b>" if result.position else ""
    plaza_says = f"\nPlaza says: {html.escape(result.message)}" if result.message else ""
    if result.ok:
        tg.send(f"✅ Applied to {label} in {result.seconds:.1f}s.{position}{plaza_says}", reply_to=message_id)
    else:
        tg.send(f"⚠️ Sent the reaction for {label}, but Plaza doesn't confirm it.{plaza_says}\n"
                "Check “Mijn reacties” on the website.", reply_to=message_id)


def check_safely(source: Source, store: Store, tg: Telegram, filters: Filters, announce: bool,
                 config: dict) -> None:
    """Run one check and handle errors/backoff for this source only."""
    if time.time() < source.paused_until:
        return
    try:
        check(source, store, tg, filters, announce, config)
        if source.backoff or source.failures >= WARN_AFTER_FAILURES:
            notify(tg, f"✅ {source.name} is reachable again, back to normal checking.")
        source.backoff = source.failures = 0
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in BLOCKED_STATUSES:
            record_failure(tg, source, e)
            return
        if not source.backoff:
            notify(tg, f"⚠️ {source.name} answered {e.response.status_code} — probably rate limiting. "
                       "Slowing down for that site; I'll tell you when it works again.")
        source.backoff = min(source.backoff * 2 or BACKOFF_START, BACKOFF_MAX)
        retry_after = e.response.headers.get("Retry-After", "")
        wait = max(source.backoff, int(retry_after) if retry_after.isdigit() else 0)
        source.paused_until = time.time() + wait
        log.warning("%s blocked with %s; pausing it for %d min", source.name, e.response.status_code, wait // 60)
    except Exception as e:
        record_failure(tg, source, e)


MAX_LIST_BUTTONS = 20  # keep the keyboard under a long list usable


def send_lists(tg: Telegram, sources: list[Source], store: Store, config: dict) -> None:
    """One message (or a few, if long) per site, with an Apply button per listing where possible."""
    for s in sources:
        buttons = [
            (f"⚡ Apply: {l.address}"[:60], APPLY_PREFIX + l.key)
            for l in s.latest
            if can_apply(l, config) and not store.has_applied(l.key)
        ][:MAX_LIST_BUTTONS]
        try:
            tg.send(format_list(f"Currently available on {s.name}", s.latest), buttons=buttons or None)
        except httpx.HTTPError as e:
            log.warning("Could not send %s list: %s", s.name, e)


def sleep_and_answer_commands(seconds: float, tg: Telegram, sources: list[Source], store: Store,
                              config: dict) -> None:
    """Wait until the next check, reacting instantly to commands and button taps (long polling)."""
    by_command = {f"/{s.site}": s for s in sources}
    end = time.time() + seconds
    while (remaining := end - time.time()) > 0:
        try:
            for ev in tg.events(timeout=int(min(remaining, LONG_POLL_SECONDS))):
                if ev["type"] == "button":
                    if ev["data"].startswith(APPLY_PREFIX):
                        tg.answer_callback(ev["callback_id"], "Applying…")
                        handle_apply(tg, store, config, ev["data"][len(APPLY_PREFIX):], ev["message_id"],
                                     ev["keyboard"])
                    else:
                        tg.answer_callback(ev["callback_id"])
                elif ev["command"] == "/list":
                    send_lists(tg, sources, store, config)
                elif ev["command"] in by_command:
                    send_lists(tg, [by_command[ev["command"]]], store, config)
                elif ev["command"] in ("/start", "/help"):
                    site_cmds = "\n".join(f"{c} – only {s.name}" for c, s in by_command.items())
                    tg.send(f"Commands:\n/list – everything that matches your filters, per site\n{site_cmds}")
        except TelegramError as e:
            if e.status == CONFLICT:
                warn_about_second_copy(tg)
            else:
                log.warning("%s", e)
            time.sleep(min(COMMAND_POLL_SECONDS, max(0, end - time.time())))
        except httpx.HTTPError as e:
            log.warning("Telegram poll failed: %s", type(e).__name__)
            time.sleep(min(COMMAND_POLL_SECONDS, max(0, end - time.time())))


_second_copy_warned = False


def warn_about_second_copy(tg: Telegram) -> None:
    global _second_copy_warned
    log.warning("409 Conflict: another program is polling this Telegram bot")
    if not _second_copy_warned:
        _second_copy_warned = True
        notify(tg, "⚠️ Another copy of the watcher is running with this bot. Only one of them receives "
                   "your button taps and commands — stop the other one.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="check once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    filters = load_filters(config)
    interval = config.get("watch", {})
    enabled = config.get("sources", {}).get("enabled", list(FETCHERS))
    sources = [Source(site, FETCHERS[site]) for site in enabled]
    tg = Telegram.from_env()
    if not tg.chat_id:
        raise SystemExit("❌ No TELEGRAM_CHAT_ID yet. Run: uv run python -m house_agent.telegram")
    store = Store()
    skip_old_updates(tg)

    apply_enabled = config.get("apply", {}).get("enabled", False) and "plaza" in enabled
    if apply_enabled:
        plaza_session.on_login_failure = lambda reason: notify(
            tg, f"⚠️ Plaza login failed: {html.escape(reason)}\nApply buttons won't work until it succeeds. "
                f"I'll try again in {LOGIN_RETRY_SECONDS // 60} min (check PLAZA_USERNAME/PASSWORD in .env).")
        warm_up_login()  # log in now, not when you tap a button in the start-up list

    first_round = True  # first round: a summary per site instead of per-listing messages
    while True:
        if apply_enabled:
            warm_up_login(SESSION_KEEPALIVE_SECONDS)  # cheap check; re-login only if it expired
        for source in sources:
            check_safely(source, store, tg, filters, announce=not first_round, config=config)
        if first_round:
            send_lists(tg, sources, store, config)
            first_round = False
        if args.once:
            break
        wait = random.uniform(interval.get("interval_min", 60), interval.get("interval_max", 120))
        sleep_and_answer_commands(wait, tg, sources, store, config)


def skip_old_updates(tg: Telegram) -> None:
    """Skip commands/taps sent while we weren't running.

    Right after a restart the previous copy's long poll can still be open for a few seconds,
    which Telegram answers with 409 Conflict — so wait that out instead of crashing.
    """
    deadline = time.time() + LONG_POLL_SECONDS + 10
    while True:
        try:
            tg.get_updates()
            return
        except TelegramError as e:
            if e.status != CONFLICT or time.time() > deadline:
                raise SystemExit(
                    "❌ Another program is polling this Telegram bot (409 Conflict). "
                    "Only one watcher can run per bot — stop the other copy (another terminal, a server…)."
                    if e.status == CONFLICT else f"❌ {e}"
                )
            log.warning("Telegram says another copy is still polling; waiting for it to stop…")
            time.sleep(5)


def record_failure(tg: Telegram, source: Source, error: Exception) -> None:
    log.exception("%s check failed; will retry next round", source.name)
    source.failures += 1
    if source.failures == WARN_AFTER_FAILURES:
        notify(tg, f"⚠️ The last {source.failures} {source.name} checks failed ({type(error).__name__}). "
                   "The site may have changed something — check the logs.")


def notify(tg: Telegram, text: str) -> None:
    """Best-effort status message; never let Telegram trouble crash the watcher."""
    try:
        tg.send(text)
    except httpx.HTTPError as e:
        log.warning("Could not send Telegram status: %s", e)


if __name__ == "__main__":
    main()
