"""Tests for velocity.assess_session and parse_duration_to_minutes."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from inference_router.velocity import (  # noqa: E402
    BurnMode,
    assess_session,
    parse_duration_to_minutes,
)


def expect_eq(label, got, want):
    if got != want:
        return f"  {label}: got {got!r}, want {want!r}"
    return None


def run() -> int:
    failures = []

    # parse_duration
    cases = [
        ("1 hr 27 min", 87),
        ("4 hr", 240),
        ("15 min", 15),
        ("0 hr 5 min", 5),
        ("", None),
        ("nonsense", None),
    ]
    for text, want in cases:
        got = parse_duration_to_minutes(text)
        if got != want:
            failures.append(f"  parse_duration({text!r}): got {got}, want {want}")

    # assess_session
    cases_sess = [
        # (pct_used, resets_in, expected_mode)
        (None, None, BurnMode.UNKNOWN),
        (0, "1 hr 27 min", BurnMode.AGGRESSIVE),    # 100% / 87m = 1.15 %/min
        (28, "1 hr 27 min", BurnMode.AGGRESSIVE),   # 72% / 87m = 0.83 %/min
        (80, "1 hr", BurnMode.NORMAL),              # 20% / 60m = 0.33 %/min (NORMAL band)
        (85, "1 hr", BurnMode.NORMAL),              # 15% / 60m = 0.25 %/min
        (95, "4 hr", BurnMode.CONSERVATIVE),        # 5% / 240m = 0.021 %/min
        (90, "15 min", BurnMode.BURN_IT),           # under 30m
        (50, "29 min", BurnMode.BURN_IT),
        (50, None, BurnMode.NORMAL),                # unknown duration → NORMAL
    ]
    for pct, dur, want_mode in cases_sess:
        got = assess_session(pct, dur)
        if got.mode != want_mode:
            failures.append(
                f"  assess_session(pct={pct}, dur={dur!r}): "
                f"got mode={got.mode}, want {want_mode} ({got.explanation})"
            )

    # Boundary check: 50% used at 87 min → 50/87 = 0.575 — should be aggressive
    h = assess_session(50, "1 hr 27 min")
    if h.mode != BurnMode.AGGRESSIVE:
        failures.append(f"  50/87 boundary: got {h.mode}, want AGGRESSIVE (vel={h.velocity})")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f)
        return 1
    print("PASS: velocity tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
