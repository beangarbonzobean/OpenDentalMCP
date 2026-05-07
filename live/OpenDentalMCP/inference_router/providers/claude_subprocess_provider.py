"""Claude Code subprocess provider — invokes `claude -p` to drain Max-plan
quota for non-interactive batch work.

V1 is text-only. Image support deferred (subprocess image plumbing is brittle;
better handled by the Agent SDK migration in Phase 2.5).

Not zero-cost in latency: each call boots a fresh agent harness (~5–10 s).
For high-throughput pipelines, prefer LOCAL_OLLAMA. For one-off agentic work
with quota to spare, this is how we use what's pre-paid.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from inference_router.providers.base import InferenceResult, Provider, ProviderError


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_TIMEOUT = int(os.environ.get("ROUTER_CLAUDE_TIMEOUT", "120"))


class ClaudeSubprocessProvider(Provider):
    name = "claude_subprocess"

    def call(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        model_hint: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: int = 60,
        allowed_tools: Optional[list[str]] = None,  # legacy provider, ignored
        cwd: Optional[str] = None,                   # legacy provider, ignored
    ) -> InferenceResult:
        if images:
            raise ProviderError("subprocess provider doesn't support images in v1")

        if not shutil.which(CLAUDE_BIN):
            raise ProviderError(f"`{CLAUDE_BIN}` not on PATH")

        # Use the actual timeout passed by the caller, capped at our default
        # to avoid runaway agents when something goes wrong.
        run_timeout = min(max(timeout, DEFAULT_TIMEOUT), 600)

        cmd = [CLAUDE_BIN, "-p", prompt]
        if model_hint:
            cmd += ["--model", model_hint]

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=run_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"claude subprocess timed out after {run_timeout}s") from e
        except FileNotFoundError as e:
            raise ProviderError(f"claude binary missing: {e}") from e
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if proc.returncode != 0:
            raise ProviderError(
                f"claude subprocess exit {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:300]}"
            )

        text = (proc.stdout or "").strip()
        if not text:
            raise ProviderError("claude subprocess returned empty output")

        return InferenceResult(
            text=text,
            provider=self.name,
            model=model_hint or "max-plan-default",
            input_tokens=0,   # subprocess doesn't expose token counts in v1
            output_tokens=0,
            cost_usd=0.0,     # drains pre-paid Max quota, no marginal $
            latency_ms=elapsed_ms,
            metadata={"timeout_s": run_timeout},
        )
