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
    Response,
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


async def open_contactout_search(page: Page, name: str) -> Response | None:
    """Navigate `page` to a ContactOut name search and wait for it to settle.

    Returns the main-resource Response so callers can inspect the HTTP status
    (e.g. detect 429/403 rate limits even when the page body looks plausible).
    """
    url = f"{CONTACTOUT_SEARCH_URL}?nm={name.replace(' ', '+')}&page=1"
    response = await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    return response


async def is_cloudflare_challenge(page: Page) -> bool:
    """True if `page` is stuck on a Cloudflare "Just a moment..." interstitial.

    ContactOut intermittently serves these to headless Chromium; when it does,
    no search UI ever renders, so every downstream selector silently returns
    empty. Detect explicitly so callers can fail loud instead of reporting
    zero profiles.
    """
    title = (await page.title()) or ""
    if title.strip().lower().startswith("just a moment"):
        return True
    try:
        body = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return False
    snippet = body[:500].lower()
    return (
        "performing security verification" in snippet
        or "checking your browser" in snippet
    )


async def detect_page_block(page: Page, response: Response | None = None) -> str | None:
    """Return a short error description if the current page is an unexpected
    block (CF challenge, rate-limit error, HTTP error, login redirect), or
    None if it looks like real ContactOut content.

    The returned string is meant to be embedded in a tool error message so
    upstream layers (runner, frontend) can pattern-match and surface the
    right toast.
    """
    if await is_cloudflare_challenge(page):
        return "Cloudflare bot challenge"
    if "/login" in page.url:
        return "Login redirect (session expired)"
    if response is not None and response.status >= 400:
        text = (response.status_text or "").strip()
        return f"HTTP {response.status}" + (f" {text}" if text else "")
    try:
        body = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return None
    snippet = body[:600].lower()
    if "too many requests" in snippet or "rate limit" in snippet:
        return "Rate limited (Too Many Requests)"
    return None


async def get_profile_count(page: Page) -> str:
    """Return the human-readable result count, e.g. `"1 - 6 of 11 profiles"`,
    or `"0 profiles"` when ContactOut renders a no-results banner. Empty
    string only when the page is mid-load (counter element absent)."""
    counter = page.locator("text=/of \\d+ profiles?/i").first
    if await counter.count() > 0:
        return (await counter.inner_text()).strip()
    # No counter — check for the empty-result banner before giving up so the
    # agent can distinguish "filter zeroed everything" from "still loading".
    body = page.locator("body")
    try:
        text = await body.inner_text(timeout=500)
    except PlaywrightTimeout:
        return ""
    if re.search(r"No (matching )?profiles|No results", text, re.I):
        return "0 profiles"
    return ""


async def get_result_preview(page: Page, limit: int = 8) -> list[str]:
    """Return a short text snippet per visible profile card (name + location + job).

    Picks the bio column out of each card identified by the
    ``border-b border-gray-200 p-4`` class combo. The card root's textContent
    is polluted by a hidden React tooltip ``<style>`` block (~2KB of CSS), so
    we drill one level down to the bio child and skip the contact column.
    """
    return await page.evaluate(
        """(limit) => {
            const out = [];
            const cardEls = Array.from(document.querySelectorAll('div'))
                .filter(el => {
                    const cls = (el.className || '').toString();
                    return cls.includes('border-b')
                        && cls.includes('border-gray-200')
                        && cls.includes('p-4');
                });
            for (const card of cardEls) {
                let bio = '';
                for (const child of card.children) {
                    if (child.tagName === 'INPUT' || child.tagName === 'STYLE') continue;
                    if (child.querySelector && child.querySelector('style')) continue;
                    const t = (child.innerText || child.textContent || '').trim();
                    if (!t) continue;
                    if (t.includes('View email') || t.includes('Find phone')) continue;
                    if (t.length > bio.length) bio = t;
                }
                if (!bio) continue;
                out.push(bio.replace(/\\s+/g, ' ').trim().slice(0, 300));
                if (out.length >= limit) break;
            }
            return out;
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


async def extract_visible_profiles(
    page: Page, snippet_limit: int = 500, max_profiles: int = 8
) -> list[dict]:
    """Per-card data: ``[{"snippet": "<name + location + current/past roles>",
    "emails": ["..."]}]``.

    Run after ``reveal_visible_emails`` so emails are present in the DOM.

    ContactOut profile cards are direct children of the results container,
    identified by the class combo ``border-b border-gray-200 p-4``. Each card
    contains (in order): a checkbox, a *bio column* (name + location + work
    history rows), a *contact column* (View email button / revealed email +
    phone), and a hidden tooltip ``<style>`` block. The bio column is what
    the user sees on screen — short, structured, no CSS. We isolate it by
    picking the card child whose text doesn't include "View email"/"Find
    phone" / ``<style>``, and emails are pulled from the whole card.
    """
    return await page.evaluate(
        """({snippetLimit, maxProfiles, emailPattern}) => {
            const emailRe = new RegExp(emailPattern, 'g');
            const filterEmails = (txt) => {
                const seen = new Set();
                const out = [];
                for (const m of txt.matchAll(emailRe)) {
                    const e = m[0];
                    if (e.startsWith('***')) continue;
                    if (e.includes('contactout.com')) continue;
                    if (e.toLowerCase().includes('sentry')) continue;
                    if (e.includes('@2x')) continue;
                    if (seen.has(e)) continue;
                    seen.add(e);
                    out.push(e);
                }
                return out;
            };

            // Cards: every <div> whose class contains border-b + border-gray-200 + p-4.
            // That's the consistent boundary for one profile result row.
            const cardEls = Array.from(document.querySelectorAll('div'))
                .filter(el => {
                    const cls = (el.className || '').toString();
                    return cls.includes('border-b')
                        && cls.includes('border-gray-200')
                        && cls.includes('p-4');
                });

            const cards = [];
            for (const card of cardEls) {
                // Pick the bio child: the one with text that doesn't include
                // the contact-button labels and isn't a STYLE element.
                let bio = '';
                for (const child of card.children) {
                    if (child.tagName === 'INPUT' || child.tagName === 'STYLE') continue;
                    if (child.querySelector && child.querySelector('style')) continue;
                    const t = (child.innerText || child.textContent || '').trim();
                    if (!t) continue;
                    if (t.includes('View email') || t.includes('Find phone')) continue;
                    if (t.length > bio.length) bio = t;
                }
                if (!bio) continue;
                const snippet = bio.replace(/\\s+/g, ' ').trim().slice(0, snippetLimit);

                // Emails: scan via innerText (respects display boundaries) so
                // adjacent "Copy" button labels don't fuse onto the email.
                const cardText = card.innerText || card.textContent || '';
                const emails = filterEmails(cardText);

                cards.push({snippet, emails});
                if (cards.length >= maxProfiles) break;
            }
            return cards;
        }""",
        {
            "snippetLimit": snippet_limit,
            "maxProfiles": max_profiles,
            "emailPattern": EMAIL_RE.pattern,
        },
    )


async def apply_filters_and_search(
    page: Page, *, location: str | None, company: str | None
) -> bool:
    """Apply optional location/company filters, click Search, wait for refresh.
    Returns True if any filter was applied.

    Instead of sleeping a fixed 3.5s after clicking Search (which often races
    the result-list reload and leaves the page mid-refresh — counter element
    missing, no cards rendered yet), we capture the pre-click `1 - N of M
    profiles` text and poll for it to *change*. Bounded at 8s — if the page
    never settles, we fall back to a short fixed wait so the caller can read
    whatever state is there.
    """
    applied = False
    before_count = await get_profile_count(page)

    if location:
        if await _apply_react_select_filter(page, "Location", location):
            applied = True
    if company:
        if await _apply_react_select_filter(page, "Company", company):
            applied = True
    if not applied:
        return False

    await page.wait_for_timeout(500)
    search_btn = page.locator("button:has-text('Search')").first
    try:
        await search_btn.scroll_into_view_if_needed()
        await search_btn.click(timeout=4000)
    except PlaywrightTimeout:
        # Click never landed — let the caller read whatever's there; the
        # MCP tool will detect the unchanged count and the agent can recover.
        return applied

    try:
        # `wait_for_function` polls every ~50ms (Playwright default) until the
        # predicate returns truthy or the timeout fires. We accept three end
        # states: counter text changed (results refreshed), counter present
        # with same text after 2s (filter was a no-op), or a "no results"
        # banner appeared (legitimate empty result).
        await page.wait_for_function(
            """(before) => {
                const txt = document.body.innerText || '';
                if (/No results|No matching profiles/i.test(txt)) return true;
                const m = txt.match(/1 - \\d+ of \\d+ profiles?|of \\d+ profile/i);
                if (!m) return false;          // page still loading, counter missing
                return m[0] !== before;        // counter updated → results refreshed
            }""",
            arg=before_count or "",
            timeout=8000,
        )
    except PlaywrightTimeout:
        # Last-ditch settle wait; the caller can still recover from a stale
        # count by re-querying.
        await page.wait_for_timeout(1500)

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
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
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
