"""Per-run time breakdown of agent_runner logs.

Reads the N most-recently-modified `.agent_logs/*.log` files and splits each
run's wall time into:
  - lock_wait      time spent blocked on _BROWSER_LOCK before work starts
  - tool:<name>    time each MCP / built-in tool spent executing
  - model          everything else inside the run window (model thinking +
                   token generation between tool results)

Tool windows are inferred from the log lines `agent_runner.write_log` emits:
  TS [tool] name=<X> input=<...>          (tool call dispatched)
  TS [sdk] UserMessage: ... ToolResultBlock(...)   (result returned)

A tool's duration = next ToolResultBlock timestamp − [tool] line timestamp.
Anything outside any tool window is attributed to `model`.

Usage:
  python3 scripts/log_time_breakdown.py            # last 10 logs
  python3 scripts/log_time_breakdown.py 20         # last 20 logs
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / ".agent_logs"

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
TOOL_CALL_RE = re.compile(r"\[tool\] name=(\S+)")
TOOL_RESULT_RE = re.compile(r"\[sdk\] UserMessage:.*ToolResultBlock\(")


def parse_ts(line: str) -> dt.datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")


def analyze(path: Path) -> dict | None:
    """Return a dict with per-event timings, or None if the log is unusable."""
    start_ts: dt.datetime | None = None       # [runner] start
    acquired_ts: dt.datetime | None = None    # [runner] acquired browser lock (only present if it had to wait)
    end_ts: dt.datetime | None = None         # [runner] done / aborting / exception
    waited = False                            # saw "waiting for browser lock"

    # (tool_name, call_ts) entries waiting for the next ToolResultBlock
    pending_call: tuple[str, dt.datetime] | None = None
    tool_durations: dict[str, float] = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            ts = parse_ts(raw)
            if not ts:
                continue
            rest = raw[20:]   # everything after the timestamp + space

            if "[runner] start" in rest:
                start_ts = ts
                continue
            if "[runner] waiting for browser lock" in rest:
                waited = True
                continue
            if "[runner] acquired browser lock" in rest:
                acquired_ts = ts
                continue
            if "[runner] done" in rest or "[runner] aborting" in rest or "[runner] exception" in rest:
                end_ts = ts
                # If we had a pending tool call without a result, close it here.
                if pending_call:
                    name, t0 = pending_call
                    tool_durations[name] = tool_durations.get(name, 0.0) + (ts - t0).total_seconds()
                    pending_call = None
                continue

            m = TOOL_CALL_RE.search(rest)
            if m:
                # If there's an unmatched previous call, ignore it (shouldn't
                # really happen — tools are serial in this skill).
                pending_call = (m.group(1), ts)
                continue

            if pending_call and TOOL_RESULT_RE.search(rest):
                name, t0 = pending_call
                tool_durations[name] = tool_durations.get(name, 0.0) + (ts - t0).total_seconds()
                pending_call = None

    if start_ts is None or end_ts is None:
        return None

    # Work window starts when the lock is acquired (or at the runner start if
    # no wait happened) and ends at runner-done.
    work_start = acquired_ts if waited and acquired_ts else start_ts
    work_total = (end_ts - work_start).total_seconds()
    if work_total <= 0:
        return None

    tool_total = sum(tool_durations.values())
    model_time = max(0.0, work_total - tool_total)
    lock_wait = (acquired_ts - start_ts).total_seconds() if waited and acquired_ts else 0.0

    return {
        "path": path,
        "address_log": path.name,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "work_total": work_total,
        "model_time": model_time,
        "lock_wait": lock_wait,
        "tools": tool_durations,
    }


# Friendly short names for the chart.
SHORT_NAME = {
    "mcp__landlord__whoownswhat_lookup": "whoownswhat_lookup",
    "mcp__landlord__contactout_open":    "contactout_open",
    "mcp__landlord__contactout_query":   "contactout_query",
    "mcp__landlord__contactout_apply_filters": "contactout_apply_filters",
    "mcp__landlord__contactout_reveal_and_read": "contactout_reveal_and_read",
    "mcp__landlord__contactout_close":   "contactout_close",
    "mcp__landlord__contactout_search":  "contactout_search",
}


def bar(frac: float, width: int = 40) -> str:
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(s, 60)
    return f"{int(m)}m{int(s):02d}s"


def render(run: dict) -> str:
    lines: list[str] = []
    addr = run["address_log"]
    total = run["work_total"]
    lines.append(
        f"{addr}  work={fmt_secs(total)}"
        + (f"  (waited {fmt_secs(run['lock_wait'])} for lock)" if run["lock_wait"] else "")
    )

    rows: list[tuple[str, float]] = []
    for name, secs in run["tools"].items():
        rows.append((SHORT_NAME.get(name, name), secs))
    rows.sort(key=lambda r: r[1], reverse=True)
    rows.append(("model (thinking+gen)", run["model_time"]))

    label_w = max(len(r[0]) for r in rows)
    for label, secs in rows:
        frac = secs / total if total else 0
        lines.append(
            f"  {label.ljust(label_w)}  {bar(frac)}  {fmt_secs(secs):>7}  ({frac*100:4.1f}%)"
        )
    return "\n".join(lines)


def render_aggregate(runs: list[dict]) -> str:
    """Average breakdown across runs."""
    if not runs:
        return ""
    totals: dict[str, float] = {}
    work_sum = 0.0
    model_sum = 0.0
    for r in runs:
        work_sum += r["work_total"]
        model_sum += r["model_time"]
        for name, secs in r["tools"].items():
            totals[SHORT_NAME.get(name, name)] = totals.get(SHORT_NAME.get(name, name), 0.0) + secs
    lines = [f"\nAGGREGATE across {len(runs)} runs  total work={fmt_secs(work_sum)}"]
    rows = sorted(totals.items(), key=lambda r: r[1], reverse=True)
    rows.append(("model (thinking+gen)", model_sum))
    label_w = max(len(n) for n, _ in rows)
    for label, secs in rows:
        frac = secs / work_sum if work_sum else 0
        lines.append(
            f"  {label.ljust(label_w)}  {bar(frac)}  {fmt_secs(secs):>7}  ({frac*100:4.1f}%)  avg/run {fmt_secs(secs/len(runs))}"
        )
    return "\n".join(lines)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    paths = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    chosen = paths[:n]
    runs = []
    for p in chosen:
        r = analyze(p)
        if r is None:
            print(f"{p.name}: skipped (no start/done markers)", file=sys.stderr)
            continue
        runs.append(r)
    print(render_aggregate(runs).lstrip())


if __name__ == "__main__":
    main()
