"""End-to-end test of the landlord-lookup agent setup.

Mirrors the SDK configuration in `agent_runner.py` (same model, inlined
SKILL.md, eagerly-loaded MCP schemas, no `Skill`/`ToolSearch`) so this
script is the place to sanity-check a single address before running the
full app.

Run with:
    .venv/bin/python scripts/test_landlord_lookup.py
    .venv/bin/python scripts/test_landlord_lookup.py "82 Union Avenue, Brooklyn"

Requires a ContactOut login cached at
~/.claude/playwright-profiles/landlord-lookup — run
`.venv/bin/python scripts/contactout_search.py --login` once if you haven't.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

# Pull the live SDK config from agent_runner so this script always tracks
# whatever the production runner uses. If you change MODEL / ALLOWED_TOOLS
# / the inlined skill there, this test picks it up automatically.
from agent_runner import ALLOWED_TOOLS, MODEL, _SKILL_PROMPT
from landlord_lookup_tool import landlord_mcp


DEFAULT_PROMPT = "82 Union Avenue, Brooklyn"


def _tool_result_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    return ""


async def main(prompt: str) -> None:
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env or export it.")

    print(f"[test] model={MODEL}")
    print(f"[test] prompt={prompt!r}")
    print(f"[test] skill prompt: {len(_SKILL_PROMPT)} chars inlined into system_prompt")
    print()

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=MODEL,
            setting_sources=["user", "project", "local"],
            mcp_servers={"landlord": landlord_mcp},
            tools=list(ALLOWED_TOOLS),
            allowed_tools=ALLOWED_TOOLS,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _SKILL_PROMPT,
            },
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            print("[init] tools:", message.data.get("tools"))
            print("[init] mcp_servers:", message.data.get("mcp_servers"))
            print()
        elif isinstance(message, AssistantMessage):
            for block in message.content or []:
                if isinstance(block, ToolUseBlock):
                    print(f"→ tool: {block.name} {block.input}")
                elif isinstance(block, TextBlock) and block.text.strip():
                    print(block.text)
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    text = _tool_result_text(block)
                    # Truncate so the test output is readable; full result is
                    # available in the raw SDK stream if you ever need it.
                    if len(text) > 600:
                        text = text[:600] + f"... ({len(text)} chars total)"
                    print(f"← result: {text}")
        elif isinstance(message, ResultMessage):
            print("\n=== FINAL ===")
            print(message.result)


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_PROMPT
    asyncio.run(main(prompt))
