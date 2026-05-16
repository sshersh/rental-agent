"""Playwright-based landlord lookup for a NYC address.

Usage:
    .venv/bin/python scripts/landlord_lookup_playwright.py "1044 Madison Avenue"
    .venv/bin/python scripts/landlord_lookup_playwright.py --login    # one-time ContactOut sign-in

The first --login run opens ContactOut in a visible window so you can sign in
manually. Cookies and session storage persist under
~/.claude/playwright-profiles/landlord-lookup, so subsequent runs reuse the login.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contactout_search import PROFILE_DIR, search_contactout


async def lookup_whoownswhat(context: BrowserContext, address: str) -> dict:
    page = await context.new_page()
    await page.goto("https://whoownswhat.justfix.org/en/", wait_until="domcontentloaded")

    search = page.locator("input.geosuggest__input")
    await search.wait_for(timeout=15000)
    await search.click()
    await search.fill(address)
    await page.wait_for_timeout(1800)

    suggestion = page.locator("#downshift-0-menu li").first
    try:
        await suggestion.wait_for(timeout=6000)
        await suggestion.click()
    except PlaywrightTimeout:
        await search.press("ArrowDown")
        await page.wait_for_timeout(300)
        await search.press("Enter")

    await page.wait_for_url(re.compile(r"/address/"), timeout=20000)
    await page.wait_for_load_state("networkidle")
    for _ in range(6):
        await page.mouse.wheel(0, 600)
        await page.wait_for_timeout(300)
    await page.wait_for_timeout(800)

    text = await page.locator("body").inner_text()

    info: dict = {"address": address, "wow_url": page.url}

    section = re.search(
        r"Who[’']s the landlord of this building\?(.+?)Last registered",
        text,
        re.DOTALL,
    )
    landlords: list[str] = []
    if section:
        for raw in section.group(1).splitlines():
            line = raw.strip()
            if not line or line.lower().startswith("learn more"):
                continue
            landlords.append(line)
    info["landlords"] = landlords

    for pattern, key in [
        (r"Units\s+(\d+)", "units"),
        (r"Year Built\s+(\d+)", "year_built"),
        (r"Change in rent stabilized units\s+(.+)", "rent_stabilized_change"),
        (r"Open Violations\s+(\d+)", "open_violations"),
        (r"Total Violations\s+(\d+)", "total_violations"),
        (r"Eviction Filings\s+(\S+)", "eviction_filings"),
        (r"associated with\s+(\d+)\s+buildings?", "portfolio_size"),
    ]:
        m = re.search(pattern, text)
        if m:
            info[key] = m.group(1).strip()

    await page.close()
    return info


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address", nargs="?", default="1044 Madison Avenue")
    parser.add_argument(
        "--location",
        default="New York",
        help='ContactOut location filter (default: "New York"). Pass empty string to disable.',
    )
    parser.add_argument("--login", action="store_true", help="Open ContactOut for manual sign-in, then exit.")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
        )

        if args.login:
            page = await context.new_page()
            await page.goto("https://contactout.com/login")
            print("Sign in to ContactOut in the opened window, then press Enter here.", flush=True)
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            await context.close()
            return

        result: dict = {}
        try:
            wow = await lookup_whoownswhat(context, args.address)
            result["whoownswhat"] = wow

            owner = next(iter(wow.get("landlords") or []), "")
            if owner:
                co = await search_contactout(
                    context, owner, location=(args.location or None)
                )
                result["contactout"] = co
            else:
                result["contactout"] = {"skipped": "no owner extracted"}
        finally:
            await context.close()

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
