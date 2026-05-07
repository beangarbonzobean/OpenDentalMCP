"""Parser tests for scraper.parse_claude_usage().

Real-page text fixture captured 2026-05-04 from claude.ai/settings/usage.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running this file directly with python.
_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent.parent
sys.path.insert(0, str(_PKG))

from utilization_dashboard.scraper import parse_claude_usage  # noqa: E402


SAMPLE_TEXT = """Title: Claude
URL: https://claude.ai/settings/usage
Source element: <main>
---
General
Account
Privacy
Billing
Usage
Capabilities
Connectors
Claude Code
Claude in Chrome
Beta
Plan usage limits
Max (20x)
Current session
Resets in 1 hr 49 min
28% used
Weekly limits
Learn more about usage limits
All models
Resets Tue 3:00 PM
23% used
Sonnet only
Resets Tue 3:00 PM
0% used
Claude Design
Resets Tue 3:00 PM
20% used
Last updated: less than a minute ago
Additional features
Daily included routine runs
You haven't run any routines yet
0 / 15
Extra usage
Turn on extra usage to keep using Claude if you hit a limit. Learn more
$8.74 spent
Resets Jun 1
9% used
$100
Monthly spend limit
Adjust limit
$71.27
Current balance·
Auto-reload
Off
Buy extra usage
Up to 30% off
"""


def run() -> int:
    parsed = parse_claude_usage(SAMPLE_TEXT)
    failures = []

    expected = {
        "plan": "Max (20x)",
        "session_pct": 28.0,
        "session_resets_in_text": "1 hr 49 min",
        "weekly_all_pct": 23.0,
        "weekly_sonnet_pct": 0.0,
        "weekly_design_pct": 20.0,
        "weekly_resets_at_text": "Tue 3:00 PM",
        "daily_routines_used": 0,
        "daily_routines_cap": 15,
        "api_extra_spent_usd": 8.74,
        "api_extra_cap_usd": 100.0,
        "api_extra_pct": 9.0,
        "api_extra_balance_usd": 71.27,
        "api_extra_resets_text": "Jun 1",
    }
    for k, v in expected.items():
        if parsed.get(k) != v:
            failures.append(f"  {k}: expected {v!r}, got {parsed.get(k)!r}")

    if "session_resets_at_iso" not in parsed:
        failures.append("  session_resets_at_iso: missing")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f)
        print("\nFull parse:")
        for k in sorted(parsed):
            if k != "raw_text":
                print(f"  {k}: {parsed[k]!r}")
        return 1
    print(f"PASS: parsed {sum(1 for k in parsed if k != 'raw_text')} fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
