---
name: landlord-lookup
description: Use this skill whenever the user provides a NYC address (with or without a borough) or asks anything about who owns a building, landlord contact info, or owner emails for a NYC property. Looks the address up on Who Owns What, then on ContactOut for each individual owner, and returns one consolidated JSON record.
---

Use the tools on the `landlord` SDK MCP server. Do not drive a browser
manually; the tools own all Playwright details.

**Address lookup:**

- `mcp__landlord__whoownswhat_lookup({ address })`

**ContactOut — shared page, per-owner lock:**

- `mcp__landlord__contactout_acquire({})` → `{ lock_token }`
- `mcp__landlord__contactout_query({ lock_token, name })` → `{ profile_count, preview }`
- `mcp__landlord__contactout_apply_filters({ lock_token, location, company })` → `{ profile_count, preview }`
- `mcp__landlord__contactout_reveal_and_read({ lock_token })` → `{ profile_count, profiles, emails }`
- `mcp__landlord__contactout_release({ lock_token })`

A single ContactOut page is shared across all concurrent agents. Acquire the
lock **per owner**, do the query/filter/reveal sequence, then release before
you reason about candidates. The lock auto-releases after 120 seconds — if a
later tool call returns `stale lock_token`, just `contactout_acquire` again
and restart that owner.

The browser is always visible (Cloudflare rejects headless Chromium).
Tools no longer take a `headless` argument.

## Step 1: Who Owns What

Call `whoownswhat_lookup` with the user's address string (include the borough if
provided, e.g. `"82 Union Avenue, Brooklyn"`). Capture from the result:

- `landlords` — list of names (individuals + LLCs, all jumbled)
- `units`, `year_built`, `open_violations`, `total_violations`,
  `eviction_filings`, `rent_stabilized_change`, `portfolio_size`, `wow_url`
- `portfolio` — list of associated buildings

## Step 2: Split individuals from LLCs

LLCs and management companies don't have personal emails. Split `landlords` into
two buckets:

- **llcs**: any name ending in (case-insensitive) `LLC`, `L.L.C.`, `INC`,
  `INC.`, `CORP`, `CO.`, `REALTY`, `ASSOCIATES`, `MANAGEMENT`, `PROPERTIES`,
  `HOLDINGS`, `PARTNERS`, `GROUP`, `TRUST`.
- **individuals**: everything else.

If the individuals bucket is empty, fall back to also searching the most
person-shaped LLC name (e.g. `"Judson Realty LLC"` → search `"Judson"`).

## Step 3: ContactOut for each individual

A single ContactOut page is shared across all concurrent agents. You claim
exclusive access **per owner** by calling `contactout_acquire` before the
query/filter/reveal sequence and `contactout_release` immediately after
`reveal_and_read`. Reasoning about candidates and ranking emails happens
**outside** the lock so other agents aren't blocked while you think.

### 3a. The per-owner lock contract

For **each** owner:

1. Call `contactout_acquire({})`. It may block briefly while another agent
   finishes — that's expected. Save the returned `lock_token`.
2. Run query → (maybe filters) → `reveal_and_read`, threading `lock_token`
   through every call.
3. Call `contactout_release({ lock_token })` as soon as `reveal_and_read`
   returns. Do not hold the lock while picking the candidate or ranking
   emails.

**Stale-token recovery.** If any tool returns an error mentioning
`"Stale lock_token"`, the 120-second watchdog auto-released the lock (or
another agent reclaimed it). Discard the token, call `contactout_acquire`
again, and re-do **this owner's** sequence from scratch. Don't retry more
than twice for the same owner — if it keeps timing out, record the owner
with `emails: []` and move on.

**Hard-fail on any other block.** If any ContactOut tool returns an `error`
that starts with `"ContactOut blocked:"` for a reason **other than** stale
tokens or Cloudflare (e.g. `"Rate limited (Too Many Requests)"`,
`"HTTP 4xx/5xx"`, `"Login redirect"`), stop calling ContactOut immediately,
call `contactout_release` if you currently hold the lock, and skip to Step 4.
The block is global, not per-owner. Return the JSON with a top-level
`"error"` field whose value is the verbatim error string from the tool.
Include whatever you collected from Who Owns What (building stats, llcs,
portfolio) and any owners completed before the block; leave the remaining
owners out of the `owners` array entirely.

### 3b. For each owner: query broadly first, filter only when needed

Iterate the individuals bucket. **Default to the broadest search and only
narrow when the result set genuinely demands it** — filters tend to zero out
valid hits, and the cost of a noisy result is much smaller than the cost of
filtering away the right person.

**1. Broad query.** Call `contactout_query({ lock_token, name })` with the
**full name** exactly as listed on WoW (first + last, e.g. `"Stephen
Judson"`). Never search by last name only. Look at the returned
`profile_count` and `preview` (one short snippet per visible card).

**2. Read or narrow? Apply this rule strictly.** Use the **total** profile
count (the second number — e.g. `"1 - 4 of 6 profiles"` → total = 6).
Filters cost a ~10s page reload **and** frequently zero out the right
person, so the bar for filtering is high:

- **`total ≤ 10`** → **Read immediately.** Skip `apply_filters` entirely.
  This applies even when count is 4–10 and the previews look mixed —
  reading a handful of extra emails is much cheaper than a filter that
  might delete the right one. Do not "clean up" small result sets.

- **`total > 10`** → **Apply a location filter.** Call
  `contactout_apply_filters({ lock_token, location: "New York",
  company: "" })`. "New York" selects the "New York, New York, United
  States" autocomplete entry, which covers all five boroughs — don't
  pre-narrow to a specific borough. Re-inspect the new `profile_count`,
  then re-apply this rule: if `total ≤ 10`, Read; if it's still > 10
  and you have an operating-shaped LLC, consider a company filter.

- **Company filter (last resort).** Only after location filtering left
  `total > 10` **and** WoW's `llcs` bucket contains a name that looks
  like an **operating company** rather than a building-specific
  single-purpose LLC. Apply with `contactout_apply_filters({
  lock_token, location: "", company: "<operating-shaped name>" })`
  (location is already applied, no need to re-pass).

A name is "operating-company shaped" if it does **not** contain the
building's street number or street name from the input address.
`PG 1044 Madison Associates LLC` is building-specific for `1044 Madison
Avenue`; `Judson Realty LLC` is operating-shaped. Strip the legal suffix
(`LLC`, `INC`, etc.) before passing as `company`. NYC buildings are
usually registered under building-specific LLCs that don't appear on
anyone's LinkedIn — that's why company filters tend to zero out and
should be a last resort.

**3. Recover from zero.** **Never apply filters to fix an empty
result** — filters can only shrink a result set, so they can't recover
hits that the name query didn't find. Filters are exclusively for
narrowing `total > 10`.

- If a **filter** pass drops `profile_count` to 0, that filter was
  wrong: re-`contactout_query({ lock_token, name })` to clear filter
  state and **Read** the broad result instead.
- If the **broad query** itself returns 0, re-`contactout_query` with
  a name variant (shortened first name, e.g. `"Benjamin"` → `"Ben"`;
  or drop a middle initial). Never drop to last-name-only, never add
  a location/company filter.

**Cap empty-search recovery at 3 total `contactout_query` calls per
owner** (the initial query + 2 retries). If all 3 come back empty,
record the owner with `emails: []` and move on to the next owner.

**4. Read, then release.** Call `contactout_reveal_and_read({
lock_token })`. Returns `profile_count`, a flat `emails` list, and
`profiles` — `[{snippet, emails}, ...]` where each `snippet` is ~800
chars of card text (name, title, company, past roles, location).
**Immediately call `contactout_release({ lock_token })`** so the next
agent can use the page. Steps 5 and 6 below run after the release.

**5. Pick the candidate.** **Single profile → always use it**, even
if the occupation looks off. Only judge when 2+ profiles tie.

- **Promote** ownership-shaped roles (founder, owner, principal,
  partner, president, C-suite, managing director), long career
  history (10+ years → old enough to have accumulated property),
  and NYC-area location.
- **Demote** junior/early-career profiles (intern, assistant,
  analyst, associate, junior) and non-NYC primary locations.
- **Cross-check occupation vs. `portfolio_size`.** Modest W-2 jobs
  (teacher, nurse, civil servant, etc.) are plausible for 1–3
  buildings if the career is long enough to imply age 50+, but
  implausible for 10+. A 20+ portfolio almost always belongs to a
  real-estate pro or otherwise high-capital career — prefer that
  candidate over an exact-name match with a junior title.
- **Co-owners with a shared last name.** If two+ owners on the same
  building share a last name (e.g. `"Sarah Judson"` +
  `"Stephen Judson"`), they're almost certainly siblings, spouses,
  or parent/child. After picking for one, prefer a profile for the
  next whose **age fits a plausible relation** — siblings/couples
  within ~15 years of each other (inferred from career length),
  parent/child 20+ years apart — plus same NYC area and often
  overlapping companies or LLCs. A 25-year-old analyst doesn't pair
  with a 70-year-old developer as a sibling. Note the relation in
  `matched_profile.reason` (e.g. "sibling of Stephen — both ~55,
  Brooklyn, shared Judson Realty history").

Equally plausible → keep both. Record pick + short reason (cite
portfolio-size logic or co-owner relation if either broke the tie)
in `matched_profile`; nothing fits → `null`.

**6. Rank emails.** Reorder the `emails` you record so the canonical
contact is first — drop nothing, just sort. Within each chosen
candidate, then non-chosen candidates after, apply:

1. Personal local-part (`first.last` / `firstname` / `flast`) on a
   free-mail domain (gmail, yahoo, hotmail, outlook, aol, icloud,
   proton, me).
2. Personal local-part on a company domain (corporate domain matching
   the LLC is itself a strong signal).
3. Initials / partial-name (`js@`, `jsilber@`).
4. Other named local-parts.
5. Generic role addresses (`info@`, `contact@`, `admin@`, `office@`,
   `hello@`, `team@`, `support@`, `noreply@`, `mail@`, `leasing@`).
   Always last.

### 3c. After the last owner

There is no "close" — the shared browser stays open for other agents.
You only ever call `contactout_release` (per owner, paired with each
acquire). After releasing the lock for the last owner, proceed directly
to Step 4.

Keep every email per owner. Don't dedupe across owners — the same email under
two owners is meaningful signal.

## Step 4: Return JSON only

Your **final response** must be a single fenced ```json``` code block and
nothing else — no prose before or after. Conform to this shape exactly:

```json
{
  "address": "<address as provided>",
  "wow_url": "<from whoownswhat_lookup>",
  "error": "<verbatim tool error string if you hard-failed on a ContactOut block, otherwise omit or null>",
  "building_stats": {
    "units": "<string or null>",
    "year_built": "<string or null>",
    "rent_stabilized_change": "<string or null>",
    "open_violations": "<string or null>",
    "total_violations": "<string or null>",
    "eviction_filings": "<string or null>",
    "portfolio_size": "<string or null>"
  },
  "owners": [
    {
      "name": "<full owner name as listed on WoW>",
      "contactout_profile_count": "<e.g. '1 - 2 of 2 profiles' or null>",
      "location_filter_used": "<e.g. 'New York' or null>",
      "company_filter_used": "<e.g. 'Judson Realty' or null>",
      "matched_profile": {
        "snippet": "<~200 chars of the picked candidate's snippet, or null>",
        "reason": "<one short line on why this candidate, or null>"
      },
      "occupation": "<single lowercase word summarizing the picked candidate's primary occupation, e.g. 'realtor', 'developer', 'landlord', 'lawyer', 'doctor', 'teacher', 'retired'; null if no profile matched>",
      "emails": ["<best>", "<...>", "<worst>"]
    }
  ],
  "llcs": ["<llc name>", "..."],
  "portfolio": [
    {
      "address": "<address>",
      "borough": "<borough>",
      "zip": "<zip>",
      "bbl": "<bbl>",
      "landlord": "<rollup landlord label, e.g. 'STEPHEN JUDSON+4'>"
    }
  ]
}
```

Rules:

- Include every individual owner in `owners`, even if their `emails` list is
  empty — emptiness is data, not failure.
- `occupation` must be a **single lowercase word** distilled from the picked
  candidate's title/industry (e.g. `"realtor"`, `"developer"`, `"landlord"`,
  `"investor"`, `"lawyer"`, `"doctor"`, `"teacher"`, `"engineer"`,
  `"retired"`, `"student"`). No spaces, no hyphens, no punctuation. If no
  profile was matched (`matched_profile` is null) or the snippet has no
  usable signal, set `occupation` to `null`.
- `llcs` is the raw list of names that matched the LLC filter, in the order WoW
  returned them.
- `portfolio` is the `portfolio` array from `whoownswhat_lookup`, passed through
  verbatim.
- Use `null` (not the literal string `"null"`) for missing scalar fields.
- Do not invent fields. If you didn't get a value, set it to `null` or an empty
  list.
