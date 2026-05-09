"""Burn-rate / headroom-velocity helpers.

A naive "% used" gauge says nothing about *how aggressive* you can be.
80% used with 4h of session left = tight. 80% used with 5min left = burn it.

Velocity (%-remaining per minute-to-reset) makes that quantitative.

A linear-pace 5h Max session burns 100% / 300min = 0.33 %/min. Any "remaining
budget rate" higher than that means we're ahead of pace and can be aggressive;
much lower means we should conserve.

Burn modes:
    BURN_IT       reset is imminent (<30 min). Use whatever's left freely.
    AGGRESSIVE    plenty of headroom relative to time. Prefer Max for
                  non-trivial work.
    NORMAL        on-pace. Default routing.
    CONSERVATIVE  tight. Reserve Max for interactive use; route batch elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Tunables — keep in one place so the dashboard can show them
# ---------------------------------------------------------------------------

BURN_IT_MINUTES = 30        # < this many minutes to reset → burn mode
AGGRESSIVE_VELOCITY = 0.5   # %/min remaining above this → aggressive
CONSERVATIVE_VELOCITY = 0.15  # %/min remaining below this → conservative


class BurnMode(str, Enum):
    BURN_IT = "burn_it"
    AGGRESSIVE = "aggressive"
    NORMAL = "normal"
    CONSERVATIVE = "conservative"
    UNKNOWN = "unknown"


@dataclass
class HeadroomReading:
    pct_remaining: float            # 100 - used
    minutes_to_reset: Optional[int]
    velocity: Optional[float]       # %/min, None if minutes_to_reset unknown
    mode: BurnMode
    explanation: str                # human-readable one-liner


def assess_session(pct_used: Optional[float], resets_in_text: Optional[str]) -> HeadroomReading:
    """Read the Claude Max current-session headroom + reset window."""
    if pct_used is None:
        return HeadroomReading(0.0, None, None, BurnMode.UNKNOWN,
                               "no scrape yet — defaulting to NORMAL")
    pct_remaining = max(0.0, 100.0 - float(pct_used))
    mins = parse_duration_to_minutes(resets_in_text or "")

    if mins is None:
        return HeadroomReading(
            pct_remaining, None, None, BurnMode.NORMAL,
            f"{pct_remaining:.0f}% remaining; reset time unknown — NORMAL")

    if mins < BURN_IT_MINUTES and pct_remaining > 0:
        return HeadroomReading(
            pct_remaining, mins, None, BurnMode.BURN_IT,
            f"reset in {mins} min — burn the remaining {pct_remaining:.0f}%")

    if mins == 0:
        return HeadroomReading(
            pct_remaining, 0, None, BurnMode.UNKNOWN,
            "reset time parsed as 0 min — defaulting to NORMAL")

    velocity = pct_remaining / mins  # %/min you can spend without going over

    if velocity >= AGGRESSIVE_VELOCITY:
        mode = BurnMode.AGGRESSIVE
        why = f"{velocity:.2f}%/min headroom — Max wide open"
    elif velocity <= CONSERVATIVE_VELOCITY:
        mode = BurnMode.CONSERVATIVE
        why = f"{velocity:.2f}%/min headroom — protect Max for interactive use"
    else:
        mode = BurnMode.NORMAL
        why = f"{velocity:.2f}%/min headroom — on pace"

    return HeadroomReading(pct_remaining, mins, velocity, mode, why)


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_HOUR_RE = re.compile(r"(\d+)\s*hr")
_MIN_RE = re.compile(r"(\d+)\s*min")


def parse_duration_to_minutes(text: str) -> Optional[int]:
    """Parse strings like '1 hr 27 min', '4 hr', '15 min'. Returns total minutes
    or None if neither hours nor minutes were found."""
    if not text:
        return None
    h_match = _HOUR_RE.search(text)
    m_match = _MIN_RE.search(text)
    if not (h_match or m_match):
        return None
    hours = int(h_match.group(1)) if h_match else 0
    minutes = int(m_match.group(1)) if m_match else 0
    return hours * 60 + minutes
