"""Watch housing sites and send matching listings to Telegram.

    uv run python -m house_agent.watch          # run forever
    uv run python -m house_agent.watch --once   # one check, then exit

On start it sends the full list of matching listings; after that only changes.
In Telegram, send /list any time for everything currently available (one message per site),
or /plaza, /fizz for a single site.
Each site is checked and backed off independently, so one site failing doesn't affect the others.
"""
import argparse
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from house_agent.filters import Filters, load_config, load_filters
from house_agent.fizz import listings as fizz
from house_agent.models import Listing
from house_agent.plaza import listings as plaza
from house_agent.store import Store
from house_agent.telegram import Telegram, format_list, format_listing

log = logging.getLogger("watch")
COMMAND_POLL_SECONDS = 5  # how often to check Telegram for /list while sleeping
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


def check(source: Source, store: Store, tg: Telegram, filters: Filters, announce: bool) -> None:
    listings = [l for l in source.fetch(filters) if filters.matches(l)]
    new, gone = store.sync(source.site, listings)
    source.latest = listings
    log.info("%s: %d matching listings, %d new, %d gone", source.name, len(listings), len(new), len(gone))
    if announce:
        for l in new:
            tg.send("🆕 " + format_listing(l))
        for l in gone:
            tg.send(f"❌ No longer listed on {source.name}: {l.address}, {l.city}")


def check_safely(source: Source, store: Store, tg: Telegram, filters: Filters, announce: bool) -> None:
    """Run one check and handle errors/backoff for this source only."""
    if time.time() < source.paused_until:
        return
    try:
        check(source, store, tg, filters, announce)
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


def send_lists(tg: Telegram, sources: list[Source]) -> None:
    """One message (or a few, if long) per site."""
    for s in sources:
        notify(tg, format_list(f"Currently available on {s.name}", s.latest))


def sleep_and_answer_commands(seconds: float, tg: Telegram, sources: list[Source]) -> None:
    by_command = {f"/{s.site}": s for s in sources}
    end = time.time() + seconds
    while time.time() < end:
        try:
            for cmd in tg.commands():
                if cmd == "/list":
                    send_lists(tg, sources)
                elif cmd in by_command:
                    send_lists(tg, [by_command[cmd]])
                elif cmd in ("/start", "/help"):
                    site_cmds = "\n".join(f"{c} – only {s.name}" for c, s in by_command.items())
                    tg.send(f"Commands:\n/list – everything that matches your filters, per site\n{site_cmds}")
        except httpx.HTTPError as e:
            log.warning("Telegram poll failed: %s", e)
        time.sleep(min(COMMAND_POLL_SECONDS, max(0, end - time.time())))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="check once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    filters = load_filters(config)
    interval = config.get("watch", {})
    enabled = config.get("sources", {}).get("enabled", list(FETCHERS))
    sources = [Source(site, FETCHERS[site]) for site in enabled]
    tg = Telegram.from_env()
    if not tg.chat_id:
        raise SystemExit("❌ No TELEGRAM_CHAT_ID yet. Run: uv run python -m house_agent.telegram")
    store = Store()
    tg.get_updates()  # skip commands sent while we weren't running

    first_round = True  # first round: a summary per site instead of per-listing messages
    while True:
        for source in sources:
            check_safely(source, store, tg, filters, announce=not first_round)
        if first_round:
            send_lists(tg, sources)
            first_round = False
        if args.once:
            break
        wait = random.uniform(interval.get("interval_min", 60), interval.get("interval_max", 120))
        sleep_and_answer_commands(wait, tg, sources)


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
