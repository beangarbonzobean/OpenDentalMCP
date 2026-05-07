"""Decision-tree tests using injected QuotaSnapshot — no DB needed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from inference_router.decision import (  # noqa: E402
    Profile,
    ProviderChoice,
    route,
)
from inference_router.snapshot import QuotaSnapshot  # noqa: E402
from inference_router.velocity import BurnMode  # noqa: E402


SCENARIOS = [
    # 1) Local fits → always local, period.
    {
        "name": "local_fits_dominates",
        "profile": Profile(fits_local=True),
        "snap": QuotaSnapshot(session_pct=50, session_resets_in_text="2 hr"),
        "expect_choice": ProviderChoice.LOCAL_OLLAMA,
    },
    # 2) Image present, no local fit → API (subprocess can't do images)
    {
        "name": "image_no_local_to_api",
        "profile": Profile(has_image=True, fits_local=False),
        "snap": QuotaSnapshot(session_pct=20, session_resets_in_text="1 hr",
                              api_extra_pct=10),
        "expect_choice": ProviderChoice.ANTHROPIC_API,
    },
    # 3) Aggressive headroom + text-only + non-local → subprocess
    {
        "name": "aggressive_text_to_subprocess",
        "profile": Profile(fits_local=False),
        "snap": QuotaSnapshot(session_pct=10, session_resets_in_text="1 hr",
                              api_extra_pct=5),
        "expect_choice": ProviderChoice.CLAUDE_SUBPROCESS,
    },
    # 4) Burn mode (<30 min to reset) → subprocess regardless
    {
        "name": "burn_mode_to_subprocess",
        "profile": Profile(fits_local=False),
        "snap": QuotaSnapshot(session_pct=80, session_resets_in_text="20 min",
                              api_extra_pct=10),
        "expect_choice": ProviderChoice.CLAUDE_SUBPROCESS,
    },
    # 5) Normal headroom + not high-end + text → API (preserve Max for interactive)
    {
        "name": "normal_low_priority_to_api",
        "profile": Profile(fits_local=False, prefers_high_end=False),
        "snap": QuotaSnapshot(session_pct=80, session_resets_in_text="1 hr",
                              api_extra_pct=10),  # 20%/60m = 0.33 = NORMAL band
        "expect_choice": ProviderChoice.ANTHROPIC_API,
    },
    # 6) Normal headroom + high-end → subprocess
    {
        "name": "normal_high_end_to_subprocess",
        "profile": Profile(fits_local=False, prefers_high_end=True),
        "snap": QuotaSnapshot(session_pct=80, session_resets_in_text="1 hr",
                              api_extra_pct=10),
        "expect_choice": ProviderChoice.CLAUDE_SUBPROCESS,
    },
    # 7) Conservative headroom + text → API
    {
        "name": "conservative_to_api",
        "profile": Profile(fits_local=False),
        "snap": QuotaSnapshot(session_pct=95, session_resets_in_text="3 hr",
                              api_extra_pct=10),
        "expect_choice": ProviderChoice.ANTHROPIC_API,
    },
    # 8) API at cap, conservative Max → subprocess as last ditch
    {
        "name": "api_at_cap_subprocess_last_ditch",
        "profile": Profile(fits_local=False),
        "snap": QuotaSnapshot(session_pct=95, session_resets_in_text="3 hr",
                              api_extra_pct=99),
        "expect_choice": ProviderChoice.CLAUDE_SUBPROCESS,
    },
    # 9) Latency-sensitive → API (no subprocess overhead)
    {
        "name": "latency_sensitive_to_api",
        "profile": Profile(fits_local=False, latency_sensitive=True),
        "snap": QuotaSnapshot(session_pct=10, session_resets_in_text="1 hr",
                              api_extra_pct=5),
        "expect_choice": ProviderChoice.ANTHROPIC_API,
    },
    # 10) No scrape yet (UNKNOWN) → API
    {
        "name": "no_scrape_to_api",
        "profile": Profile(fits_local=False),
        "snap": QuotaSnapshot(),
        "expect_choice": ProviderChoice.ANTHROPIC_API,
    },
    # 11) Everything tapped → UNAVAILABLE
    {
        "name": "everything_tapped",
        "profile": Profile(fits_local=False, has_image=True),
        "snap": QuotaSnapshot(session_pct=99.5, session_resets_in_text="3 hr",
                              api_extra_pct=99),
        "expect_choice": ProviderChoice.UNAVAILABLE,
    },
]


def run() -> int:
    failures = []
    for sc in SCENARIOS:
        decision = route(sc["profile"], snap=sc["snap"])
        if decision.choice != sc["expect_choice"]:
            failures.append(
                f"  {sc['name']}: got {decision.choice.value} ({decision.reasoning}), "
                f"want {sc['expect_choice'].value}"
            )
    if failures:
        print("FAIL:")
        for f in failures:
            print(f)
        return 1
    print(f"PASS: {len(SCENARIOS)} decision scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
