## 05/19

### **Refactor: Parallel Landlord Lookups**

**Features**
- N landlord-lookup agents run in parallel against the Claude API
- Configurable "Max parallel landlord lookups" number input in settings (default 3)
- Colored job-tracker log lines (timestamp blue, event-type label yellow)

**Architecture**
- Single asyncio loop on a daemon thread, Dash callbacks dispatch via `run_coroutine_threadsafe` — replaces process-wide `threading.Lock` + per-call `asyncio.run`
- One shared Playwright context for all agents; ContactOut page guarded by an `asyncio.Lock` exposed to the agent as `acquire`/`release` tools — keeps model reasoning off-lock so only ~10–30s of page I/O serializes
- 120s watchdog auto-releases stale locks so a crashed agent can't deadlock the pool

---

### **New Feature: Added Email**

**Features**
- Email draft view with LLM generate/refine
- Gmail SMTP send to shortlisted owners
- Sent-status tracking in landlords table

**Architecture**
- Direct Anthropic SDK over `claude-agent-sdk` — simple text-gen doesn't need an agent loop
- Two-layer template persistence: JSON on the server, `localStorage` for in-progress edits
- `dcc.Store` signal pattern decouples the send action from UI refresh

### **Owner Pick Heuristics + Occupation Field**

**Features**
- Owner cards display a single-word occupation chip (e.g. `realtor`, `architect`) under each name
- Agent only applies ContactOut filters when a name-only search returns more than 10 hits
- Agent recognizes shared-last-name co-owners as plausible siblings/spouses/parent-child and picks profiles with consistent age ranges

**Architecture**
- New single-word `occupation` field persisted alongside the matched-profile snippet/reason — auditable picks, plus a presentable chip
- Skill prompt: filters only narrow `total > 10`, never recover from zero; empty-search retries capped at 3 — bounds runaway agent loops
- Skill prompt: `occupation` vs. `portfolio_size` is the tie-breaker when multiple profiles match — modest W-2 jobs implausible for large portfolios

### **Hardening: CF Resilience + Filter Backoff**

**Features**
- Manual Cloudflare unlock folded into the lookup flow — browser opens visibly, user clicks the Turnstile checkbox, lookup resumes automatically
- Toasts surface the real block reason verbatim (CF challenge, 429, login redirect) instead of fake-success on empty emails
- Agent recovers from over-filtered zero-hit searches by re-querying broad instead of dead-ending

**Architecture**
- Forced headed Chromium + `--disable-blink-features=AutomationControlled` and stripped `--enable-automation` — Cloudflare rejects `cf_clearance` issued to headless contexts and ignores Turnstile clicks from automation-flagged browsers
- Single `detect_page_block` chokepoint covers CF, login redirect, HTTP 4xx/5xx, and rate-limit body text; `ContactOut blocked:` sentinel bubbles through every MCP tool and re-raises in the runner as `ContactOutCloudflareError` so the agent can't paper over empty results with a success JSON
- Agent tool palette restricted to `tools=["Skill", "ToolSearch"]` — the `claude_code` system-prompt preset was silently injecting Bash/Read/Edit on top of the MCP server
- Filter escalation moved from rule-based (`>10 hits → always narrow`) to judgment-based (broad → narrow only on real ambiguity, recover from zero) — reverses the earlier hard threshold that was dropping valid matches

---

## 05/18

### **Optimization: Faster Lookups + Candidate Ranking**

**Features**
- Per-candidate career bios surfaced alongside emails so the agent picks the most plausible landlord
- Best-email ranking per owner (personal+free-mail > personal+company > initials > other names > generic)
- Job tracker streams `thinking`, `assistant`, `user`, and `tool_result` blocks — not just tool calls
- Model identity logged at the top of every per-BBL log line stream

**Architecture**
- Switched landlord-lookup to `claude-haiku-4-5` over Sonnet — workflow is mechanical (tools + thresholds + JSON), so Sonnet's reasoning premium isn't worth the model-time cost
- Inlined `SKILL.md` and pre-listed MCP tool names in `tools=` to skip `Skill` / `ToolSearch` round-trips at agent boot
- Event-driven filter wait via `page.wait_for_function` over a fixed-timer sleep — replaces a 3500ms race against the SPA
- Ranking heuristic shifted from WoW-LLC name overlap to candidate plausibility (seniority, age, NYC ties) — better signal on small candidate sets

---
