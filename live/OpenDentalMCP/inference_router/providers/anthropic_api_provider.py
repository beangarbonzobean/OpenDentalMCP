"""Anthropic API provider — billable. Hits the user's API key.

Uses the `anthropic` SDK if installed; raises ProviderError otherwise so
the router can fall through.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Optional

from inference_router.providers.base import InferenceResult, Provider, ProviderError


# Pricing for Haiku 4.5 — same as ocr_helper.py
HAIKU_INPUT_USD_PER_TOKEN = 1.00 / 1_000_000
HAIKU_OUTPUT_USD_PER_TOKEN = 5.00 / 1_000_000

DEFAULT_MODEL = os.environ.get(
    "ROUTER_ANTHROPIC_MODEL",
    "claude-haiku-4-5-20251001",
)


class AnthropicAPIProvider(Provider):
    name = "anthropic_api"

    def __init__(self) -> None:
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:
            raise ProviderError("anthropic SDK not installed") from e
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def call(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        model_hint: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: int = 60,
        allowed_tools: Optional[list[str]] = None,  # API-side tool use not exposed in v1
        cwd: Optional[str] = None,                   # not applicable
    ) -> InferenceResult:
        client = self._get_client()
        model = model_hint or DEFAULT_MODEL

        content: list[dict] = []
        for img in images or []:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(img).decode("ascii"),
                },
            })
        content.append({"type": "text", "text": prompt})

        t0 = time.perf_counter()
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
                timeout=timeout,
            )
        except Exception as e:
            raise ProviderError(f"anthropic API call failed: {type(e).__name__}: {e}") from e
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text

        in_tok = int(getattr(resp.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(resp.usage, "output_tokens", 0) or 0)
        cost = in_tok * HAIKU_INPUT_USD_PER_TOKEN + out_tok * HAIKU_OUTPUT_USD_PER_TOKEN

        return InferenceResult(
            text=text.strip(),
            provider=self.name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=round(cost, 6),
            latency_ms=elapsed_ms,
            metadata={"stop_reason": getattr(resp, "stop_reason", None)},
        )
