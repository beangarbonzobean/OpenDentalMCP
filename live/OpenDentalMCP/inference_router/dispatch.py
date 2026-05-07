"""Decide-and-call top-level. Logs the decision, dispatches, walks the
fallback chain on ProviderError, and updates the log row with the outcome.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Optional

from inference_router import log_store, snapshot as snap_mod
from inference_router.decision import Profile, ProviderChoice, RouteDecision, route
from inference_router.providers.anthropic_api_provider import AnthropicAPIProvider
from inference_router.providers.base import InferenceResult, Provider, ProviderError
from inference_router.providers.claude_agent_sdk_provider import ClaudeAgentSDKProvider
from inference_router.providers.claude_subprocess_provider import ClaudeSubprocessProvider
from inference_router.providers.ollama_provider import OllamaProvider

log = logging.getLogger(__name__)


# Default provider per choice. CLAUDE_MAX uses the Agent SDK (low-overhead);
# the subprocess implementation stays available as a manual override via
# ROUTER_USE_SUBPROCESS=1 for debugging.
def _claude_max_provider() -> Provider:
    if os.environ.get("ROUTER_USE_SUBPROCESS") == "1":
        return ClaudeSubprocessProvider()
    return ClaudeAgentSDKProvider()


_PROVIDERS: dict[ProviderChoice, Provider] = {
    ProviderChoice.LOCAL_OLLAMA: OllamaProvider(),
    ProviderChoice.ANTHROPIC_API: AnthropicAPIProvider(),
    ProviderChoice.CLAUDE_MAX: _claude_max_provider(),
}


def dispatch(
    profile: Profile,
    prompt: str,
    *,
    images: Optional[list[bytes]] = None,
    max_tokens: int = 2048,
    timeout: int = 120,
) -> InferenceResult:
    """Route to a provider, call it, fall through on failure. Logs both
    decision and outcome to utilization.db.

    Raises ProviderError only if every provider in the chain failed.
    """
    decision = route(profile)
    fallbacks = [decision.choice] + list(decision.fallback_chain)

    log_ts = log_store.log_decision(
        choice=decision.choice.value,
        burn_mode=decision.burn_mode.value,
        reasoning=decision.reasoning,
        profile_tag=profile.tag,
        profile_json=json.dumps(asdict(profile)),
        fallbacks=[c.value for c in decision.fallback_chain],
    )

    last_err: Optional[Exception] = None
    for idx, choice in enumerate(fallbacks):
        if choice == ProviderChoice.UNAVAILABLE:
            continue
        provider = _PROVIDERS.get(choice)
        if provider is None:
            continue
        try:
            result = provider.call(
                prompt,
                images=images,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except ProviderError as e:
            last_err = e
            log.warning("router: %s failed (%s); trying next in chain", choice.value, e)
            continue

        outcome = "ok" if idx == 0 else "fallback"
        log_store.update_outcome(
            log_ts,
            outcome=outcome,
            provider_used=result.provider,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        return result

    log_store.update_outcome(
        log_ts,
        outcome="failed",
        error=str(last_err)[:500] if last_err else "all providers exhausted",
    )
    raise ProviderError(
        f"all providers in chain failed: {', '.join(c.value for c in fallbacks)}: {last_err}"
    )
