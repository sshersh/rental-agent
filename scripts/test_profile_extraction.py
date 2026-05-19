"""Dump per-candidate profile data straight from ContactOut, no agent involved.

Drives the same Playwright pipeline the MCP tools use:
  open browser → search by name → (optional) apply filters → reveal emails →
  extract_visible_profiles → pretty-print.

Run:
    .venv/bin/python scripts/test_profile_extraction.py "Joel Silberstein"
    .venv/bin/python scripts/test_profile_extraction.py "Joel Silberstein" --location "New York"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from scripts.contactout_search import (  # noqa: E402  (sys.path tweak above)
    PROFILE_DIR,
    apply_filters_and_search,
    detect_page_block,
    extract_visible_emails,
    extract_visible_profiles,
    get_profile_count,
    get_result_preview,
    is_cloudflare_challenge,
    open_contactout_search,
    reveal_visible_emails,
)

# Mirror landlord_lookup_tool's stealth launch flags so Cloudflare behaves
# the same way it does in production.
_STEALTH = {
    "args": ["--disable-blink-features=AutomationControlled"],
    "ignore_default_args": ["--enable-automation"],
}


async def main(name: str, location: str | None, company: str | None) -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            **_STEALTH,
        )
        try:
            page = await ctx.new_page()
            print(f"[test] opening dashboard for {name!r}")
            response = await page.goto(
                "https://contactout.com/dashboard/search", wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(2000)

            if await is_cloudflare_challenge(page):
                print("[test] Cloudflare challenge — solve it in the visible window…")
                for _ in range(30):
                    await page.wait_for_timeout(1500)
                    if not await is_cloudflare_challenge(page):
                        break

            block = await detect_page_block(page, response)
            if block:
                print(f"[test] page blocked: {block}")
                return

            print(f"[test] searching name={name!r}")
            await open_contactout_search(page, name)
            block = await detect_page_block(page)
            if block:
                print(f"[test] page blocked: {block}")
                return

            count_before = await get_profile_count(page)
            preview = await get_result_preview(page)
            print(f"[test] profile_count (before filters): {count_before}")
            print(f"[test] preview ({len(preview)} cards):")
            for i, snippet in enumerate(preview):
                print(f"  [{i}] {snippet[:180]}")
            print()

            if location or company:
                print(f"[test] applying filters location={location!r} company={company!r}")
                await apply_filters_and_search(page, location=location, company=company)
                count_after = await get_profile_count(page)
                print(f"[test] profile_count (after filters): {count_after}")
                print()

            print("[test] revealing emails…")
            await reveal_visible_emails(page)

            print("[test] extracting profiles…")
            profiles = await extract_visible_profiles(page)
            flat_emails = await extract_visible_emails(page)

            print()
            print("=" * 80)
            print(f"profiles ({len(profiles)} cards)")
            print("=" * 80)
            for i, p in enumerate(profiles):
                print(f"\n[{i}] emails: {p.get('emails')}")
                snippet = p.get("snippet") or ""
                print(f"    snippet ({len(snippet)} chars):")
                # Wrap snippet to ~100 cols for readability.
                line_w = 100
                for j in range(0, len(snippet), line_w):
                    print(f"      {snippet[j:j+line_w]}")
            print()
            print("=" * 80)
            print(f"flat emails ({len(flat_emails)}): {flat_emails}")
            print("=" * 80)

            # Also dump as JSON for downstream inspection.
            print()
            print("[test] raw JSON:")
            print(json.dumps({"profiles": profiles, "emails": flat_emails}, indent=2))

        finally:
            await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="Person name to search on ContactOut")
    ap.add_argument("--location", default=None, help='Location filter (e.g. "New York")')
    ap.add_argument("--company", default=None, help='Company filter')
    args = ap.parse_args()
    asyncio.run(main(args.name, args.location, args.company))
