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


# ---------------------------------------------------------------------------
# JSON path — the "API" the page itself calls
# ---------------------------------------------------------------------------
#
# The /settings/usage page hits GET /api/organizations/<uuid>/usage which
# returns structured JSON. Way more robust than scraping rendered text:
#  - Reset times are absolute ISO timestamps (no relative-duration parsing)
#  - Sonnet bucket and Claude Design ("omelette") are explicit fields
#  - New plan tiers appear as new keys without the parser breaking
#
# Codename map (Anthropic-internal -> our schema):
#   five_hour                  -> session
#   seven_day                  -> weekly all-models
#   seven_day_sonnet           -> weekly sonnet-only
#   seven_day_omelette         -> weekly Claude Design
#   extra_usage                -> $X / $100 monthly cap

def parse_claude_usage_json(usage: dict, prepaid_credits: dict | None = None) -> dict:
    """Translate the API JSON into our existing Claude snapshot schema.

    Mirrors the field names produced by parse_claude_usage() so the dashboard
    UI doesn't need to change. Pass `prepaid_credits` if you fetched
    /api/organizations/<id>/prepaid/credits separately (the balance shown
    on the page lives there, not in /usage).
    """
    out: dict = {"raw_text": _summarize_for_debug(usage, prepaid_credits)}

    # We don't have the plan name here, but the page header shows "Max (20x)".
    # Best-effort: leave the existing plan field if a previous scrape set it;
    # caller (writer) is INSERT OR REPLACE on (ts) so missing field is fine.

    five_hour = usage.get("five_hour") or {}
    if "utilization" in five_hour:
        out["session_pct"] = float(five_hour["utilization"])
    if five_hour.get("resets_at"):
        out["session_resets_at_iso"] = _normalize_iso(five_hour["resets_at"])
        out["session_resets_in_text"] = _format_relative(five_hour["resets_at"])

    seven_day = usage.get("seven_day") or {}
    if "utilization" in seven_day:
        out["weekly_all_pct"] = float(seven_day["utilization"])
    if seven_day.get("resets_at"):
        out["weekly_resets_at_text"] = _format_weekly_when(seven_day["resets_at"])

    sonnet = usage.get("seven_day_sonnet") or {}
    if "utilization" in sonnet:
        out["weekly_sonnet_pct"] = float(sonnet["utilization"])

    omelette = usage.get("seven_day_omelette") or {}
    if "utilization" in omelette:
        out["weekly_design_pct"] = float(omelette["utilization"])

    # Opus has a dedicated weekly bucket separate from "All models" on plans
    # where Opus access is provisioned. When the field exists and is non-null
    # we surface it; some plan tiers send `null` for buckets they don't gate.
    opus = usage.get("seven_day_opus") or {}
    if isinstance(opus, dict) and "utilization" in opus:
        out["weekly_opus_pct"] = float(opus["utilization"])

    # Cowork (Claude Code background routines) — also its own bucket on Max.
    cowork = usage.get("seven_day_cowork") or {}
    if isinstance(cowork, dict) and "utilization" in cowork:
        out["weekly_cowork_pct"] = float(cowork["utilization"])

    extra = usage.get("extra_usage") or {}
    if extra:
        # used_credits and monthly_limit are in cents per the API.
        if "used_credits" in extra:
            out["api_extra_spent_usd"] = float(extra["used_credits"]) / 100.0
        if "monthly_limit" in extra:
            out["api_extra_cap_usd"] = float(extra["monthly_limit"]) / 100.0
        if "utilization" in extra:
            out["api_extra_pct"] = float(extra["utilization"])

    # Balance lives in a separate endpoint.
    if prepaid_credits:
        balance_cents = prepaid_credits.get("balance_credits")
        if balance_cents is None:
            # Some response shapes use a different field name; try a few.
            balance_cents = (prepaid_credits.get("balance")
                             or prepaid_credits.get("available_credits"))
        if balance_cents is not None:
            try:
                out["api_extra_balance_usd"] = float(balance_cents) / 100.0
            except (TypeError, ValueError):
                pass

    return out


def _normalize_iso(ts: str) -> str:
    """Strip sub-second precision and ensure trailing Z form, return ISO string."""
    # Anthropic returns "2026-05-07T23:00:00.112462+00:00" — keep as-is, our
    # downstream parsing handles the +00:00 / Z forms equivalently.
    return ts


def _format_relative(resets_at_iso: str) -> str:
    """Convert absolute reset time -> 'X hr Y min' relative to now."""
    try:
        # Python <3.11 doesn't accept "Z" suffix; normalize to +00:00
        cleaned = resets_at_iso.replace("Z", "+00:00")
        target = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return ""
    now = datetime.now(timezone.utc)
    delta = (target - now).total_seconds()
    if delta <= 0:
        return "0 min"
    hours, rem = divmod(int(delta), 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


def _format_weekly_when(resets_at_iso: str) -> str:
    """Convert absolute reset time -> 'Tue 3:00 PM' style display string.
    Local time. Falls back to raw ISO on parse failure."""
    try:
        cleaned = resets_at_iso.replace("Z", "+00:00")
        target = datetime.fromisoformat(cleaned).astimezone()
    except (ValueError, TypeError):
        return resets_at_iso
    # Cross-platform: format the parts separately to avoid Linux %-I /
    # Windows %#I divergence.
    day = target.strftime("%a")
    hour = target.strftime("%I").lstrip("0") or "12"   # 12-hour, drop pad
    rest = target.strftime(":%M %p")
    return f"{day} {hour}{rest}"


def _summarize_for_debug(usage: dict, prepaid: dict | None) -> str:
    """Cheap one-line summary stored in raw_text for debugging."""
    import json as _json
    return _json.dumps({"usage": usage, "prepaid_credits": prepaid})[:4000]


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
