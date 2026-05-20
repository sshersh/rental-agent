"""Synchronous wrapper around the Claude Agent SDK that runs the
landlord-lookup skill for a single address.

Used by app.py from a background thread. Returns the parsed JSON dict the
skill emits (per its frontmatter contract), or None on failure.

Reads ANTHROPIC_API_KEY from the environment via dotenv.

Every run writes a per-BBL log file to .agent_logs/<bbl>.log so the UI can
tail it in real time. The file is truncated at the start of each run.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from landlord_lookup_tool import landlord_mcp  # noqa: E402

load_dotenv(ROOT / ".env")

ALLOWED_TOOLS = [
    "mcp__landlord__whoownswhat_lookup",
    "mcp__landlord__contactout_acquire",
    "mcp__landlord__contactout_release",
    "mcp__landlord__contactout_query",
    "mcp__landlord__contactout_apply_filters",
    "mcp__landlord__contactout_reveal_and_read",
]


# ── Shared asyncio loop ───────────────────────────────────────────────
#
# All landlord-lookup work runs on one daemon-thread event loop, started
# lazily on first call. This lets the Playwright browser context, the
# ContactOut page, and the asyncio.Lock in landlord_lookup_tool be shared
# across many concurrent agents (each agent runs as a coroutine on this
# loop). Without a shared loop the Playwright resources can't be shared
# at all — a Page is bound to the loop that created it.

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _shared_loop() -> asyncio.AbstractEventLoop:
    """Return the shared asyncio loop, starting it in a daemon thread on
    first call. Thread-safe; only the first caller does the setup."""
    global _loop
    with _loop_lock:
        if _loop is not None:
            return _loop
        ready = threading.Event()
        loop_ref: dict[str, asyncio.AbstractEventLoop] = {}

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_ref["loop"] = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=_run_loop, name="landlord-loop", daemon=True).start()
        ready.wait()
        _loop = loop_ref["loop"]
        return _loop

# ── Per-BBL log files ──────────────────────────────────────────────────

AGENT_LOGS_DIR = ROOT / ".agent_logs"
AGENT_LOGS_DIR.mkdir(exist_ok=True)

_BBL_SAFE_RE = re.compile(r"[^\w.-]")
_log_locks: dict[str, threading.Lock] = {}
_log_locks_guard = threading.Lock()


def _lock_for(bbl: str) -> threading.Lock:
    with _log_locks_guard:
        lock = _log_locks.get(bbl)
        if lock is None:
            lock = threading.Lock()
            _log_locks[bbl] = lock
        return lock


def log_path_for(bbl: str) -> Path:
    """Resolve `.agent_logs/<safe-bbl>.log`. The filename mirrors the bbl so
    callers can find it from anywhere given only the bbl."""
    safe = _BBL_SAFE_RE.sub("_", bbl or "unknown")
    return AGENT_LOGS_DIR / f"{safe}.log"


def write_log(bbl: str, msg: str) -> None:
    """Append a timestamped line to the per-bbl log file. Thread-safe;
    workers running concurrent agents write to different files but the
    primitive is robust if the same bbl gets two writers."""
    path = log_path_for(bbl)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    with _lock_for(bbl):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def reset_log(bbl: str) -> None:
    """Truncate the per-bbl log so a fresh run starts clean."""
    path = log_path_for(bbl)
    with _lock_for(bbl):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def read_log(bbl: str, max_chars: int = 50_000) -> str:
    """Read the per-bbl log; returns "" if the file doesn't exist yet.
    Truncates to the trailing `max_chars` so the UI doesn't ship megabytes
    of text on every poll once a run has produced a lot of output."""
    path = log_path_for(bbl)
    if not path.exists():
        return ""
    with _lock_for(bbl):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    if len(text) > max_chars:
        return "... (truncated) ...\n" + text[-max_chars:]
    return text


# ── Agent runner ──────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Sentinel prefix emitted by landlord_lookup_tool whenever a ContactOut tool
# detects an unexpected page (Cloudflare challenge, HTTP 429/4xx/5xx, login
# redirect, etc.). The agent typically swallows these errors and ships a
# well-formed JSON with empty emails, which the storage layer then treats as
# a successful run. Detecting the prefix directly in tool results lets the
# runner raise so the frontend surfaces an error toast instead.
BLOCKED_PREFIX = "ContactOut blocked:"

# Subset of block reasons we still treat as a "Cloudflare" toast variant
# (separate copy in the UI). Everything else falls through to a generic
# "blocked" reason.
_CLOUDFLARE_HINT = "Cloudflare"


class ContactOutCloudflareError(RuntimeError):
    """Raised when ContactOut tool calls were blocked. The `reason` attribute
    holds a short tag ("cloudflare" | "blocked") for the frontend toast."""

    def __init__(self, message: str, reason: str = "blocked", details: list[str] | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or []


def _tool_result_text(block: ToolResultBlock) -> str:
    """Flatten a ToolResultBlock.content (str | list[dict]) into a string."""
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    return ""


def _compact(s: str | None, limit: int = 1500) -> str:
    """Collapse whitespace and truncate so a block's content fits on one log
    line. The job-tracker pane tails these logs line-by-line; multi-line
    content would split the entry across "rows" with no timestamp on the
    continuation lines, which reads worse than a single truncated line."""
    if not s:
        return ""
    flat = " ".join(s.split())
    if len(flat) > limit:
        return flat[:limit] + f"... ({len(flat)} chars total)"
    return flat


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


# The shared browser context is always headed (Cloudflare rejects headless
# Chromium), so the `headed` flag on run_landlord_lookup is a no-op. Kept on
# the signature for backward compatibility with app.py / the --headed CLI
# flag, but it injects no instructions into the system prompt.
_HEADED_INSTRUCTION = ""


def _load_skill_prompt() -> str:
    """Read SKILL.md and strip its YAML frontmatter so the body can be
    appended to the system prompt directly. Inlining the skill content here
    means the agent never has to call the Skill tool to load it — saves one
    round-trip per run."""
    path = ROOT / ".claude" / "skills" / "landlord-lookup" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        # Drop the first frontmatter block.
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    return text.lstrip("\n")


_SKILL_PROMPT = _load_skill_prompt()

# Surfaced in the per-bbl log on every run; the frontend job tracker reads
# the log so users can see which model handled their lookup.
MODEL = "claude-haiku-4-5-20251001"


async def _run(address: str, headed: bool, bbl: str) -> dict | None:
    appended = _SKILL_PROMPT
    if headed:
        appended = appended + "\n\n" + _HEADED_INSTRUCTION
    system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": appended,
    }

    final = ""
    blocked_reasons: list[str] = []   # human-readable reasons from tool errors
    async for message in query(
        prompt=address,
        options=ClaudeAgentOptions(
            # Haiku 4.5 — workflow is mechanical (call tools in sequence,
            # apply documented thresholds, emit JSON), so Sonnet's extra
            # reasoning isn't earning its ~70s/run model-time cost.
            model=MODEL,
            setting_sources=["user", "project", "local"],
            mcp_servers={"landlord": landlord_mcp},
            # `tools` restricts which built-ins are available at all (the
            # claude_code system_prompt preset otherwise loads the full
            # palette — Bash/Read/Edit/Write/Glob/Grep/WebFetch/Task/etc.
            # — which this agent never needs). MCP tools come through via
            # `mcp_servers` and are deferred by default — i.e. the agent
            # sees their names but not their schemas, so it must call
            # ToolSearch before each one. Listing them explicitly in
            # `tools=` loads their schemas upfront and saves ~10–15s of
            # ToolSearch round-trips per lookup. The Skill tool is dropped
            # because SKILL.md is inlined into system_prompt above.
            tools=list(ALLOWED_TOOLS),
            # `allowed_tools` is the auto-approve permission whitelist —
            # NOT a restriction on what's available. Listing the MCP tools
            # here means they execute without permission prompts.
            allowed_tools=ALLOWED_TOOLS,
            system_prompt=system_prompt,
            permission_mode="acceptEdits",
            stderr=lambda line: write_log(bbl, f"[cli] {line.rstrip()}"),
        ),
    ):
        write_log(bbl, f"[sdk] {type(message).__name__}: {message!r}")
        # Emit structured per-block lines so the frontend job-tracker pane
        # can show a human-readable feed (model thinking, prose, tool I/O)
        # without parsing the raw SDK repr above.
        if isinstance(message, AssistantMessage):
            for block in (message.content or []):
                if isinstance(block, ToolUseBlock):
                    try:
                        args_json = json.dumps(block.input, default=str)
                    except (TypeError, ValueError):
                        args_json = str(block.input)
                    write_log(bbl, f"[tool] name={block.name} input={args_json}")
                elif isinstance(block, ThinkingBlock):
                    text = _compact(getattr(block, "thinking", None))
                    if text:
                        write_log(bbl, f"[thinking] {text}")
                elif isinstance(block, TextBlock):
                    text = _compact(getattr(block, "text", None), limit=2000)
                    if text:
                        write_log(bbl, f"[assistant] {text}")
        if isinstance(message, UserMessage):
            for block in (message.content if isinstance(message.content, list) else []):
                if isinstance(block, ToolResultBlock):
                    text = _tool_result_text(block)
                    write_log(bbl, f"[tool_result] {_compact(text, limit=1200)}")
                    if BLOCKED_PREFIX in text:
                        # Strip everything before the prefix and keep the
                        # short reason (up to the first period) so the toast
                        # can show e.g. "Rate limited (Too Many Requests)".
                        tail = text.split(BLOCKED_PREFIX, 1)[1].strip(' "\n')
                        reason = tail.split(".", 1)[0].strip()
                        blocked_reasons.append(reason or tail[:120])
                elif isinstance(block, TextBlock):
                    text = _compact(getattr(block, "text", None))
                    if text:
                        write_log(bbl, f"[user] {text}")
        if isinstance(message, ResultMessage) and message.result:
            final = message.result
    write_log(bbl, f"[sdk] final result text ({len(final)} chars): {final!r}")
    parsed = _extract_json(final)
    # An agent-emitted top-level "error" field is a hard fail regardless of
    # whether any tool result carried BLOCKED_PREFIX — the skill instructs
    # the agent to set it when it gives up early, and we want to surface
    # that to the UI even on paths where the tool itself returned a
    # non-prefixed error blob.
    agent_error = parsed.get("error") if isinstance(parsed, dict) else None
    if agent_error:
        last_reason = blocked_reasons[-1] if blocked_reasons else str(agent_error)
        short = "cloudflare" if _CLOUDFLARE_HINT in last_reason else "blocked"
        write_log(
            bbl,
            f"[runner] aborting: agent reported error={agent_error!r} "
            f"(blocked tool errors: {len(blocked_reasons)})",
        )
        raise ContactOutCloudflareError(
            f"ContactOut blocked: {last_reason}",
            reason=short,
            details=blocked_reasons or [str(agent_error)],
        )
    if blocked_reasons:
        # Tolerate blocked-page errors if the agent recovered (e.g. retried
        # headed and the user solved a CF challenge — at least one owner
        # came back with emails). Only abort if every owner the agent
        # queried ended up empty.
        owners = (parsed or {}).get("owners") or []
        recovered = any(
            o.get("emails") for o in owners if isinstance(o, dict)
        )
        if (owners and not recovered) or not parsed:
            last_reason = blocked_reasons[-1]
            short = "cloudflare" if _CLOUDFLARE_HINT in last_reason else "blocked"
            write_log(
                bbl,
                f"[runner] aborting: ContactOut blocked ({len(blocked_reasons)} tool errors); "
                f"last reason: {last_reason!r}",
            )
            raise ContactOutCloudflareError(
                f"ContactOut blocked: {last_reason}",
                reason=short,
                details=blocked_reasons,
            )
        write_log(
            bbl,
            f"[runner] ContactOut blocked {len(blocked_reasons)}x "
            f"but emails were recovered; continuing",
        )
    return parsed


def run_landlord_lookup(
    address: str, bbl: str, headed: bool = False
) -> dict | None:
    """Synchronously run the landlord-lookup agent for `address`.
    Returns the parsed agent JSON (per the skill's schema), or None on failure.
    Blocks until done; caller is responsible for running this in a thread if
    they need a non-blocking UI.

    `bbl` is the per-run log key — every SDK message and CLI stderr line is
    appended to `.agent_logs/<bbl>.log`. The log is truncated on entry.

    `headed=True` injects a system-prompt instruction telling the agent to
    pass headless=false to every browser tool call — i.e. the Chromium
    window is visible while the agent works.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")
    reset_log(bbl)
    write_log(bbl, f"[runner] model={MODEL}")
    write_log(bbl, f"[runner] start address={address!r} headed={headed}")
    # Dispatch onto the shared loop so this agent runs concurrently with any
    # others currently in flight. The browser context, ContactOut page, and
    # page-level asyncio.Lock all live on that loop.
    future = asyncio.run_coroutine_threadsafe(_run(address, headed, bbl), _shared_loop())
    try:
        result = future.result()
        write_log(bbl, f"[runner] done result_is_dict={isinstance(result, dict)}")
        return result
    except Exception as e:
        import traceback
        write_log(bbl, f"[runner] exception: {e!r}\n{traceback.format_exc()}")
        raise
