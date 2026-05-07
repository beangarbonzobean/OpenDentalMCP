"""Pure decision function: pick which provider to route a call to.

`route(profile) -> RouteDecision` reads the current snapshot from the
utilization dashboard's DB and returns a deterministic choice plus the
reasoning. No side effects, no I/O beyond the read-only DB read.

The decision tree, in priority order:

  1. If the profile fits a local model AND the local lane is reachable
     → LOCAL_OLLAMA. Sunk cost, free, no quota concerns.

  2. If the profile is text-only AND Max session has headroom
     (per the burn-mode classifier in velocity.py)
     → CLAUDE_SUBPROCESS. Drains pre-paid Max quota, which is the goal.

  3. Otherwise → ANTHROPIC_API. Billable but capped.

  4. If everything else is exhausted (API at cap, no local, no Max) →
     UNAVAILABLE. Caller decides how to handle (queue, error, drop).

Burn-mode override: when reset is imminent (<30 min) and there's still
remaining quota, route higher-tier work to CLAUDE_SUBPROCESS even if it
wouldn't otherwise qualify — quota is about to vanish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from inference_router import snapshot as snap_mod
from inference_router.velocity import BurnMode, assess_session


class ProviderChoice(str, Enum):
    LOCAL_OLLAMA = "local_ollama"
    CLAUDE_MAX = "claude_max"
    ANTHROPIC_API = "anthropic_api"
    GEMINI_API = "gemini_api"
    UNAVAILABLE = "unavailable"


@dataclass
class Profile:
    """Description of the inference task. Used by route() to pick a provider.

    Fields are conservative defaults — caller overrides only what's relevant.
    """
    fits_local: bool = False           # Can a local Ollama model handle this?
    has_image: bool = False            # Image input present?
    prefers_high_end: bool = False     # Caller prefers Sonnet/Opus quality
    latency_sensitive: bool = False    # <2s latency required (no subprocess)
    max_output_tokens: int = 2048
    tag: str = ""                      # Free-form tag for routing-history attribution


@dataclass
class RouteDecision:
    choice: ProviderChoice
    reasoning: str                     # one-liner: why this provider
    burn_mode: BurnMode
    fallback_chain: list[ProviderChoice] = field(default_factory=list)
    model_hint: Optional[str] = None   # e.g. "claude-sonnet-4-5" when Sonnet
                                       # weekly bar has headroom


# ---------------------------------------------------------------------------
# Capacity caps to honor (read from snapshot, configurable later)
# ---------------------------------------------------------------------------

API_AT_CAP_PCT = 95.0   # treat API as exhausted above this % of $100 cap
MAX_SESSION_FULL_PCT = 99.0  # >this means session is effectively maxed
SONNET_TIGHT_PCT = 80.0  # above this % of weekly Sonnet cap, stop hinting Sonnet

import os
SONNET_MODEL_NAME = os.environ.get("ROUTER_SONNET_MODEL", "claude-sonnet-4-5")


def _sonnet_hint(snap: snap_mod.QuotaSnapshot, profile: Profile) -> Optional[str]:
    """Decide whether to ask the SDK provider for Sonnet specifically.

    Reasoning: the user's Sonnet weekly cap is its own bucket, separate from
    the All-models bucket. If Sonnet is sitting at 0% and the weekly reset is
    a few days away, that's perishable pre-paid quota — burn some on tasks
    that benefit from Sonnet quality (any prefers_high_end profile).

    Returns the model name to hint, or None to let the SDK use its default.
    """
    sonnet_pct = snap.weekly_sonnet_pct
    if sonnet_pct is None:
        # No scrape data — don't hint anything specific.
        return None
    if sonnet_pct >= SONNET_TIGHT_PCT:
        # Sonnet weekly is mostly used up — don't add to it.
        return None
    if profile.prefers_high_end or sonnet_pct < 50.0:
        # Either the caller wants high-end, or there's serious headroom
        # waiting to expire. Either way, ask for Sonnet.
        return SONNET_MODEL_NAME
    return None


def route(profile: Profile, *, snap: Optional[snap_mod.QuotaSnapshot] = None) -> RouteDecision:
    """Decide where to dispatch a call. Reads snapshot from DB by default."""
    snap = snap if snap is not None else snap_mod.latest()
    headroom = assess_session(snap.session_pct, snap.session_resets_in_text)
    fallbacks: list[ProviderChoice] = []

    # 1. Local first if we can — it's free and idle GPU is always wasted.
    if profile.fits_local:
        fallbacks = [ProviderChoice.CLAUDE_MAX, ProviderChoice.ANTHROPIC_API]
        return RouteDecision(
            choice=ProviderChoice.LOCAL_OLLAMA,
            reasoning="profile fits local; sunk-cost GPU is always preferred",
            burn_mode=headroom.mode,
            fallback_chain=fallbacks,
        )

    # 2. Subprocess (Max plan) — but only if no image (subprocess image support
    #    is brittle in v1) AND not latency-sensitive AND headroom allows it.
    can_use_max = (
        not profile.has_image
        and not profile.latency_sensitive
        and headroom.mode != BurnMode.UNKNOWN
        and (snap.session_pct or 0) < MAX_SESSION_FULL_PCT
    )

    if can_use_max:
        fallbacks = [ProviderChoice.ANTHROPIC_API]
        sonnet = _sonnet_hint(snap, profile)
        sonnet_note = f" (hinting {sonnet})" if sonnet else ""

        # Burn mode: reset imminent, use what's left even for high-end work.
        if headroom.mode == BurnMode.BURN_IT:
            return RouteDecision(
                choice=ProviderChoice.CLAUDE_MAX,
                reasoning=f"BURN MODE — {headroom.explanation}{sonnet_note}",
                burn_mode=headroom.mode,
                fallback_chain=fallbacks,
                model_hint=sonnet,
            )

        # Aggressive: Max wide open, prefer it for non-trivial work.
        if headroom.mode == BurnMode.AGGRESSIVE:
            return RouteDecision(
                choice=ProviderChoice.CLAUDE_MAX,
                reasoning=f"AGGRESSIVE — {headroom.explanation}{sonnet_note}",
                burn_mode=headroom.mode,
                fallback_chain=fallbacks,
                model_hint=sonnet,
            )

        # Normal mode: route to Max for high-end requests, otherwise let it fall through.
        if headroom.mode == BurnMode.NORMAL and profile.prefers_high_end:
            return RouteDecision(
                choice=ProviderChoice.CLAUDE_MAX,
                reasoning=f"NORMAL + prefers_high_end — {headroom.explanation}{sonnet_note}",
                burn_mode=headroom.mode,
                fallback_chain=fallbacks,
                model_hint=sonnet,
            )

        # Conservative mode: reserve Max for interactive — only burn if forced
        # (which means the caller had no other option, see below)

    # 3. Anthropic API fallback — billable but functional.
    api_at_cap = (snap.api_extra_pct or 0) >= API_AT_CAP_PCT
    if not api_at_cap:
        fallbacks = [ProviderChoice.UNAVAILABLE]
        # Note Claude subprocess as a last-ditch even if conservative
        if can_use_max and headroom.mode == BurnMode.CONSERVATIVE:
            fallbacks = [ProviderChoice.CLAUDE_MAX, ProviderChoice.UNAVAILABLE]
        reasoning = (
            "image present — subprocess provider doesn't support images in v1"
            if profile.has_image
            else "latency-sensitive — subprocess overhead too high"
            if profile.latency_sensitive
            else f"Max conservative ({headroom.explanation}) — preserving for interactive"
            if headroom.mode == BurnMode.CONSERVATIVE
            else f"Max session full or unknown — {headroom.explanation}"
        )
        return RouteDecision(
            choice=ProviderChoice.ANTHROPIC_API,
            reasoning=reasoning,
            burn_mode=headroom.mode,
            fallback_chain=fallbacks,
        )

    # 4. API at cap and Max unavailable.
    # If conservative-mode Max IS still available, dip into it as last resort.
    if can_use_max:
        return RouteDecision(
            choice=ProviderChoice.CLAUDE_MAX,
            reasoning=f"API at cap ({snap.api_extra_pct:.0f}%); using Max despite conservative",
            burn_mode=headroom.mode,
            fallback_chain=[ProviderChoice.UNAVAILABLE],
            model_hint=_sonnet_hint(snap, profile),
        )

    return RouteDecision(
        choice=ProviderChoice.UNAVAILABLE,
        reasoning=f"all routes exhausted: api_pct={snap.api_extra_pct} session_pct={snap.session_pct}",
        burn_mode=headroom.mode,
        fallback_chain=[],
    )
