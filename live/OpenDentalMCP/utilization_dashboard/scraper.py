"""Parser for the text scraped from claude.ai/settings/usage.

The dashboard's scrape is piggybacked on a Claude Code session running the
Chrome MCP. Get the page text via `mcp__Claude_in_Chrome__get_page_text`,
then feed it to `parse_claude_usage()` here. Anthropic doesn't publish a
usage API for Max, so this scrape is the only path.

Pure function — takes text, returns dict — easy to test.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_claude_usage(text: str) -> dict:
    """Extract numeric quotas from claude.ai/settings/usage page text.

    Returns a dict suitable for storage.write_claude(). Keys present only
    when found (the page evolves; missing fields are tolerated).
    """
    out: dict = {"raw_text": text}

    # Plan: "Plan usage limits\nMax (20x)"
    m = re.search(r"Plan usage limits\s*\n\s*([^\n]+)", text)
    if m:
        out["plan"] = m.group(1).strip()

    # Session: "Current session\nResets in 1 hr 49 min\n28% used"
    m = re.search(
        r"Current session\s*\n\s*Resets in (.+?)\s*\n\s*(\d+)% used",
        text,
    )
    if m:
        out["session_resets_in_text"] = m.group(1).strip()
        out["session_pct"] = float(m.group(2))
        resets_at = _resolve_relative_reset(m.group(1))
        if resets_at:
            out["session_resets_at_iso"] = resets_at

    # Weekly: each section is "<label>\nResets <when>\n<N>% used"
    for label, key in [
        ("All models", "weekly_all_pct"),
        ("Sonnet only", "weekly_sonnet_pct"),
        ("Claude Design", "weekly_design_pct"),
    ]:
        m = re.search(
            rf"{re.escape(label)}\s*\n\s*Resets ([^\n]+?)\s*\n\s*(\d+)% used",
            text,
        )
        if m:
            if "weekly_resets_at_text" not in out:
                out["weekly_resets_at_text"] = m.group(1).strip()
            out[key] = float(m.group(2))

    # Daily routines:
    # "Daily included routine runs\nYou haven't run any routines yet\n0 / 15"
    # OR "Daily included routine runs\n<some other 2nd line>\n3 / 15"
    m = re.search(
        r"Daily included routine runs[^\n]*\n[^\n]*\n\s*(\d+)\s*/\s*(\d+)",
        text,
    )
    if m:
        out["daily_routines_used"] = int(m.group(1))
        out["daily_routines_cap"] = int(m.group(2))

    # Extra usage block:
    #   "$8.74 spent"   "Resets Jun 1"   "9% used"   "$100"   "Monthly spend limit"
    m = re.search(
        r"\$([0-9.]+)\s*spent\s*\n\s*Resets ([^\n]+?)\s*\n\s*(\d+)% used\s*\n\s*"
        r"\$([0-9.]+)\s*\n\s*Monthly spend limit",
        text,
    )
    if m:
        out["api_extra_spent_usd"] = float(m.group(1))
        out["api_extra_resets_text"] = m.group(2).strip()
        out["api_extra_pct"] = float(m.group(3))
        out["api_extra_cap_usd"] = float(m.group(4))

    # Balance: "$71.27\nCurrent balance"
    m = re.search(r"\$([0-9.]+)\s*\n\s*Current balance", text)
    if m:
        out["api_extra_balance_usd"] = float(m.group(1))

    return out


_HOUR_RE = re.compile(r"(\d+)\s*hr")
_MIN_RE = re.compile(r"(\d+)\s*min")


def _resolve_relative_reset(duration_text: str) -> Optional[str]:
    """Convert '1 hr 49 min' to an ISO UTC timestamp of now+offset.

    Returns None if the format is unrecognized.
    """
    h_match = _HOUR_RE.search(duration_text)
    m_match = _MIN_RE.search(duration_text)
    if not (h_match or m_match):
        return None
    hours = int(h_match.group(1)) if h_match else 0
    minutes = int(m_match.group(1)) if m_match else 0
    target = datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)
    return target.isoformat(timespec="seconds")
