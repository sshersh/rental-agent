# Rent Stabilize Finder

A Dash web app that maps every rent-stabilized building in Brooklyn and, on click, runs a Claude agent that figures out who owns the building and surfaces their public contact info — so a tenant or organizer can reach the actual landlord behind the LLC.

## What it does

- Renders ~30k Brooklyn rent-stabilized buildings on a Leaflet map, filterable by ZIP and viewport.
- On building click, kicks off an agentic landlord lookup that:
  1. Scrapes [Who Owns What](https://whoownswhat.justfix.org) for owner names + building stats (units, violations, evictions, portfolio).
  2. Splits the owner list into individuals vs. LLCs.
  3. Searches ContactOut for each individual and reveals their public emails.
  4. Returns one consolidated JSON record, cached in SQLite for 30 days.
- Lets the user compose and send an email blast to the discovered owner addresses via Resend.

## Setup

### 1. Required external services

Three accounts need to be wired up before the app is fully functional:

**ContactOut (Basic tier).** Email reveals for individual owners come from ContactOut.
- Sign up at [contactout.com](https://contactout.com) and subscribe to at least the **Basic** plan — the free tier won't reveal personal emails.
- There's no API key. The agent drives ContactOut through the headed Chromium window using a persistent browser profile. The first time a lookup runs (or whenever the session expires) the window lands on ContactOut's `/login` page; sign in manually within ~2 minutes and the session cookie is saved to the profile dir, so later runs reuse it.

**Resend (email sending + delivery webhook).** Outreach emails go out via Resend; bounce/delivery events drive automatic retry to the owner's next candidate address.
- Create an account at [resend.com](https://resend.com) and verify a sending domain (or use Resend's onboarding/sandbox address for testing).
- Create an API key → `RESEND_API_KEY`.
- Set `RESEND_FROM_ADDRESS` to an address on your verified domain (optionally `RESEND_FROM_NAME` and `RESEND_REPLY_TO`).
- In the Resend dashboard, add a **webhook** pointing at `<public-url>/webhooks/resend` (the public URL comes from ngrok below), subscribe it to the `email.delivered`, `email.bounced`, and `email.complained` events, and copy the signing secret into `RESEND_WEBHOOK_SECRET`.

**ngrok (public URL for the webhook).** Resend needs to reach your local server to deliver webhook events, so expose the app's port with a tunnel.
- Install ngrok and authenticate it: `ngrok config add-authtoken <token>`.
- Run `ngrok http 8050` (the app's port); ngrok prints a public `https://…ngrok-free.dev` URL.
- Use `<that-url>/webhooks/resend` as the Resend webhook endpoint above. Reserving a static domain saves you re-editing the Resend webhook on every restart.

### 2. Install & run

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in ANTHROPIC_API_KEY and the RESEND_* vars (RESEND_API_KEY,
                       # RESEND_FROM_ADDRESS, RESEND_WEBHOOK_SECRET)
python app.py          # the shared Chromium is always headed (Cloudflare requirement)
```

Then open <http://localhost:8050>. On the first landlord lookup, sign in to ContactOut in the Chromium window when it appears. To receive delivery/bounce events, run `ngrok http 8050` in a second terminal and keep it running alongside the app.

## Design brief

**Frontend.** A single-page Dash app (`app.py`) using `dash-leaflet` for the map and `dash-bootstrap-components` for the chrome. All filtering is done client-side over a pre-built GeoJSON of buildings loaded from `bklyn_rent_stabilized_buildings.csv`. A polling job-tracker panel tails per-lookup log files so the user can watch the agent work in real time.

**Agent layer.** `agent_runner.py` wraps the Claude Agent SDK and invokes the `landlord-lookup` skill (`.claude/skills/landlord-lookup/SKILL.md`) against `claude-haiku-4-5` — the workflow is mechanical (call tools in sequence, apply thresholds, emit JSON), so Sonnet's extra reasoning isn't worth its model-time cost. The skill body is inlined into the system prompt to skip a Skill-tool round-trip, and tool schemas are pre-loaded to skip ToolSearch round-trips.

**Tooling.** `landlord_lookup_tool.py` registers an in-process SDK MCP server (`landlord`) exposing six Playwright-backed tools: `whoownswhat_lookup` (parallel-safe — opens its own tab in the shared context) plus five ContactOut tools that all drive a single shared page. `contactout_acquire` returns a `lock_token`; the agent threads it through `query` / `apply_filters` / `reveal_and_read`; then `contactout_release` hands the page to the next agent. Acquire/release happens per owner, so reasoning about candidates and ranking emails runs off-lock and overlaps with other agents' page work.

**Browser strategy.** One `launch_persistent_context` is launched lazily on the first lookup against a shared profile dir; every subsequent agent shares that context and the cached `cf_clearance` cookie. Chromium is always headed because Cloudflare rejects clearance issued to headless Chromium — both WoW and ContactOut lookups are visible. Stealth launch flags strip the `navigator.webdriver` / `--enable-automation` fingerprints Turnstile probes for. The shared ContactOut page is guarded by an `asyncio.Lock`; a 120-second watchdog force-releases if the holding agent crashes between `acquire` and `release`, so a forgetful agent can't deadlock the rest. CF-blocked calls emit a `ContactOut blocked:` sentinel so the runner can raise instead of letting the agent return a "success" with empty emails.

**Persistence.** `agent_cache.py` is a SQLite cache with a normalized schema: `results` (one row per BBL), `landlords` (deduped by name), `building_landlords` (join), `portfolio` (one row per portfolio member). A successful lookup also fans out to every portfolio building present in the rent-stab CSV, so clicking one building from a large portfolio warms the cache for the rest.

**Concurrency.** `agent_runner.py` starts a single asyncio event loop on a daemon thread; every lookup runs as a coroutine on it via `run_coroutine_threadsafe`. N agents reason in parallel against the Claude API while serializing only at the shared ContactOut page (lock-held for ~10–30s per owner; model thinking happens off-lock). In-flight count is capped by a user-configurable "Max parallel landlord lookups" number input in the settings modal (default 3, persisted to localStorage). The frontend polls `/api/result/:bbl` and tails `.agent_logs/<bbl>.log` for live status; the job-tracker panel renders each log line with colored spans (timestamp blue, event-type label yellow) so the eye can jump between model thinking and tool I/O at a glance.
