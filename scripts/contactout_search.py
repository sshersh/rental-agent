"""Playwright-based ContactOut search with optional location filter.

Usage:
    .venv/bin/python scripts/contactout_search.py "Stephen Judson"
    .venv/bin/python scripts/contactout_search.py "Stephen Judson" --location "New York"
    .venv/bin/python scripts/contactout_search.py --login

The --login flag opens ContactOut for manual sign-in. Cookies persist under
~/.claude/playwright-profiles/landlord-lookup so future runs reuse the session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

PROFILE_DIR = Path.home() / ".claude" / "playwright-profiles" / "landlord-lookup"


async def _apply_react_select_filter(page: Page, label_text: str, value: str) -> bool:
    """Type `value` into the react-select whose label is `label_text` and pick
    the first matching autocomplete option. Returns True on success.

    Most ContactOut filters are react-selects with no stable selectors (the
    `react-select-N` index shifts), so we locate by walking up from the label's
    text node to find a sibling `input[id^="react-select-"]`.
    """
    input_id = await page.evaluate(
        """(label) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const tn = walker.currentNode;
                if ((tn.nodeValue || '').trim() === label) {
                    let p = tn.parentElement;
                    for (let i = 0; i < 8 && p; i++) {
                        const inp = p.querySelector('input[id^="react-select-"]');
                        if (inp) return inp.id;
                        p = p.parentElement;
                    }
                }
            }
            return null;
        }""",
        label_text,
    )
    if not input_id:
        return False

    field = page.locator(f"#{input_id}")
    await field.scroll_into_view_if_needed()
    await field.click()
    await field.type(value, delay=80)
    await page.wait_for_timeout(1800)

    option = page.locator('[class*="contactout-select__option"]').first
    try:
        await option.wait_for(timeout=5000)
        await option.click()
        return True
    except PlaywrightTimeout:
        return False


CONTACTOUT_SEARCH_URL = "https://contactout.com/dashboard/search"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")


async def open_contactout_search(page: Page, name: str) -> None:
    """Navigate `page` to a ContactOut name search and wait for it to settle."""
    url = f"{CONTACTOUT_SEARCH_URL}?nm={name.replace(' ', '+')}&page=1"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)


async def get_profile_count(page: Page) -> str:
    counter = page.locator("text=/of \\d+ profiles?/i").first
    if await counter.count() > 0:
        return (await counter.inner_text()).strip()
    return ""


async def get_result_preview(page: Page, limit: int = 8) -> list[str]:
    """Return a short text snippet per visible profile card (name + location + job)."""
    return await page.evaluate(
        """(limit) => {
            const cards = [];
            const seen = new Set();
            const btns = Array.from(document.querySelectorAll('*')).filter(el =>
                el.textContent && el.textContent.trim() === 'View email' && el.children.length === 0
            );
            for (const btn of btns) {
                let card = btn;
                for (let i = 0; i < 10; i++) {
                    if (!card.parentElement) break;
                    card = card.parentElement;
                    const t = card.textContent || '';
                    if (t.length < 800 && /United States|United Kingdom|Australia|New Zealand|Canada|Israel/.test(t)) {
                        if (seen.has(card)) break;
                        seen.add(card);
                        cards.push(card);
                        break;
                    }
                }
            }
            return cards.slice(0, limit).map(c =>
                (c.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300)
            );
        }""",
        limit,
    )


async def reveal_visible_emails(page: Page, limit: int = 5) -> None:
    view_btns = page.locator("button:has-text('View email'), :text('View email')")
    n = await view_btns.count()
    for i in range(min(n, limit)):
        try:
            await view_btns.nth(i).click(timeout=3000)
            await page.wait_for_timeout(900)
        except PlaywrightTimeout:
            continue


def filter_emails(raw: list[str]) -> list[str]:
    out = [
        e for e in raw
        if not e.startswith("***")
        and "contactout.com" not in e
        and "sentry" not in e.lower()
        and "@2x" not in e
    ]
    return list(dict.fromkeys(out))


async def extract_visible_emails(page: Page) -> list[str]:
    text = await page.locator("body").inner_text()
    return filter_emails(EMAIL_RE.findall(text))


async def apply_filters_and_search(
    page: Page, *, location: str | None, company: str | None
) -> bool:
    """Apply optional location/company filters, click Search, wait for refresh.
    Returns True if any filter was applied."""
    applied = False
    if location:
        if await _apply_react_select_filter(page, "Location", location):
            applied = True
    if company:
        if await _apply_react_select_filter(page, "Company", company):
            applied = True
    if applied:
        await page.wait_for_timeout(500)
        search_btn = page.locator("button:has-text('Search')").first
        try:
            await search_btn.scroll_into_view_if_needed()
            await search_btn.click(timeout=4000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_timeout(3500)
    return applied


async def search_contactout(
    context: BrowserContext,
    name: str,
    location: str | None = None,
    company: str | None = None,
    reveal_first: bool = True,
) -> dict:
    page = await context.new_page()
    url = f"https://contactout.com/dashboard/search?nm={name.replace(' ', '+')}&page=1"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)

    if "/login" in page.url or "Sign in" in (await page.title()):
        await page.close()
        return {"error": "Not logged in to ContactOut. Re-run with --login."}

    if await page.locator("text=/We couldn.+t find/i").count() > 0:
        await page.close()
        return {"error": f"No ContactOut results for {name!r}"}

    location_applied = False
    company_applied = False
    if location:
        location_applied = await _apply_react_select_filter(page, "Location", location)
    if company:
        company_applied = await _apply_react_select_filter(page, "Company", company)

    if location_applied or company_applied:
        await page.wait_for_timeout(500)
        search_btn = page.locator("button:has-text('Search')").first
        try:
            await search_btn.scroll_into_view_if_needed()
            await search_btn.click(timeout=4000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_timeout(3500)

    profile_count_text = ""
    counter = page.locator("text=/of \\d+ profiles?/i").first
    if await counter.count() > 0:
        profile_count_text = (await counter.inner_text()).strip()

    if reveal_first:
        view_btns = page.locator("button:has-text('View email'), :text('View email')")
        n = await view_btns.count()
        for i in range(min(n, 5)):
            try:
                await view_btns.nth(i).click(timeout=3000)
                await page.wait_for_timeout(900)
            except PlaywrightTimeout:
                continue

    text = await page.locator("body").inner_text()
    locations_in_results = re.findall(
        r"(?:New York|Brooklyn|Manhattan|Queens|Bronx|Staten Island)[^\n]*?United States",
        text,
    )
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", text)
    emails = [
        e for e in emails
        if not e.startswith("***")
        and "contactout.com" not in e
        and "sentry" not in e.lower()
        and "@2x" not in e
    ]
    emails = list(dict.fromkeys(emails))

    await page.close()
    return {
        "name": name,
        "location_filter": location,
        "company_filter": company,
        "profile_count": profile_count_text,
        "result_locations": locations_in_results[:8],
        "emails": emails,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", help='Person name, e.g. "Stephen Judson"')
    parser.add_argument("--location", help='e.g. "New York" or "Brooklyn"')
    parser.add_argument("--company", help='Company filter, e.g. "Judson Realty"')
    parser.add_argument("--login", action="store_true", help="Open ContactOut for manual sign-in.")
    parser.add_argument("--headless", action="store_false")
    parser.add_argument("--no-reveal", action="store_true", help="Don't click View email.")
    args = parser.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    headless = False if args.login else args.headless

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
        )

        if args.login:
            page = await context.new_page()
            await page.goto("https://contactout.com/login")
            print("Sign in to ContactOut in the browser window, then press Enter here.", flush=True)
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            await context.close()
            return

        if not args.name:
            parser.error("name is required unless --login is used")

        try:
            result = await search_contactout(
                context,
                args.name,
                location=args.location,
                company=args.company,
                reveal_first=not args.no_reveal,
            )
        finally:
            await context.close()

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
