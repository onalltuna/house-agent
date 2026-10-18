"""Plaza login: log in once with a real browser, then reuse the cookies over plain HTTP.

    uv run python -m house_agent.plaza.session            # log in (headless) and verify
    uv run python -m house_agent.plaza.session --headed   # watch the browser do it
    uv run python -m house_agent.plaza.session --check    # only check the saved session
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://plaza.newnewnew.space"
LOGIN_PAGE = f"{BASE_URL}/portaal/account"
ACCOUNT_URL = f"{BASE_URL}/portal/account/frontend/getaccount/format/json"
STATE_FILE = Path(".state/plaza_state.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    load_dotenv(".env")
    user, pw = os.getenv("PLAZA_USERNAME"), os.getenv("PLAZA_PASSWORD")
    if not user or not pw:
        raise LoginError("Set PLAZA_USERNAME and PLAZA_PASSWORD in .env")
    return user, pw


def browser_login(headed: bool = False, timeout_ms: int = 30_000) -> None:
    """Log in through the real login form and save cookies to STATE_FILE."""
    username, password = _credentials()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(user_agent=USER_AGENT, locale="nl-NL")
        page = context.new_page()
        page.goto(LOGIN_PAGE, wait_until="networkidle")

        # The page also contains a hidden login popup with the same fields, so target the visible form.
        page.locator("input[name=username]:visible").fill(username)
        page.locator("input[name=password]:visible").fill(password)
        try:
            with page.expect_response(lambda r: "loginbyservice" in r.url, timeout=timeout_ms):
                page.locator("button[type=submit]:visible", has_text="Inloggen").click()
        except PlaywrightTimeout:
            browser.close()
            raise LoginError("Login did not complete — wrong credentials? Re-run with --headed to see.")

        account = page.request.get(ACCOUNT_URL).json().get("account")
        if not account:
            browser.close()
            raise LoginError("Login request finished but Plaza reports no account session.")

        STATE_FILE.parent.mkdir(exist_ok=True)
        context.storage_state(path=STATE_FILE)
        STATE_FILE.chmod(0o600)  # it's effectively your password
        browser.close()


def http_client() -> httpx.Client:
    """An httpx client carrying the saved Plaza session cookies."""
    client = httpx.Client(
        base_url=BASE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    if STATE_FILE.exists():
        for c in json.loads(STATE_FILE.read_text())["cookies"]:
            client.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return client


def get_account(client: httpx.Client) -> dict | None:
    return client.get(ACCOUNT_URL).json().get("account")


def ensure_logged_in(headed: bool = False) -> httpx.Client:
    """Return a logged-in client, doing a browser login only if the saved session is gone."""
    client = http_client()
    if get_account(client):
        return client
    client.close()
    browser_login(headed=headed)
    client = http_client()
    if not get_account(client):
        raise LoginError("Browser login succeeded but cookies don't work over plain HTTP.")
    return client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--check", action="store_true", help="only check the saved session")
    args = parser.parse_args()

    try:
        # ensure_logged_in() already used the client, so close it explicitly instead of `with`.
        client = http_client() if args.check else ensure_logged_in(headed=args.headed)
        try:
            account = get_account(client)
        finally:
            client.close()
    except LoginError as e:
        sys.exit(f"❌ {e}")

    if not account:
        sys.exit("❌ Not logged in (no saved session, or it expired).")
    # Print only the field names so personal data doesn't end up in logs or chats.
    print(f"✅ Logged in. Account fields available: {sorted(account)[:15]}")


if __name__ == "__main__":
    main()
