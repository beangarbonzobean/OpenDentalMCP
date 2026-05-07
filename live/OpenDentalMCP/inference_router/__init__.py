"""Inference router — picks where to send a Claude/LLM call so we drain
pre-paid quota first, then fall through to billable APIs.

Goal isn't to minimize cost, it's to maximize use of capacity already paid for:
  - Local Ollama on the GPU host (sunk cost)
  - Claude Max subscription (pre-paid, perishable — session resets every 5h
    and weekly caps reset Tuesday)
  - Anthropic API extra usage (billable, capped at $100/mo)
  - Gemini API (billable, only if explicitly opted in)

Decision logic considers BOTH the % used AND the time remaining until reset
('burn rate'). When a session window is about to reset, the remaining quota
is effectively free and should be used aggressively. When reset is far away,
conserve.

Public surface:
    from inference_router import route, dispatch, Profile, ProviderChoice
    choice = route(profile)              # decision only, no I/O
    result = dispatch(profile, prompt)   # decide + call + log
"""

from inference_router.decision import (  # noqa: F401
    Profile,
    ProviderChoice,
    RouteDecision,
    route,
)
from inference_router.dispatch import dispatch  # noqa: F401

__all__ = ["Profile", "ProviderChoice", "RouteDecision", "route", "dispatch"]
