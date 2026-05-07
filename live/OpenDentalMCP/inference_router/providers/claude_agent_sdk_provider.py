"""Claude Agent SDK provider — drains Max-plan quota with no shell-out tax.

Uses `claude_agent_sdk.query()` for one-shot inference. Same Max-quota
draining as the subprocess provider, but with much lower latency overhead
(~1-2 s vs 5-10 s for subprocess) and programmable error handling.

Auth: piggybacks on the user's existing Claude Code login (the SDK reuses
the same credentials Claude Code uses on this machine — no API key needed).

Image support: deferred to a follow-up (the SDK can accept image content
blocks but our test seam isn't there yet).

Async-to-sync bridging: the SDK's `query` is an async iterator. We wrap
`asyncio.run()` for callers in plain sync code (most of our pipelines).
If you call this from inside an existing event loop you'll need a different
bridge — but our intake/classification/extraction code is all sync.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from inference_router.providers.base import InferenceResult, Provider, ProviderError


DEFAULT_MODEL_HINT = os.environ.get("ROUTER_CLAUDE_MAX_MODEL", "")  # empty = SDK default
DEFAULT_TIMEOUT = int(os.environ.get("ROUTER_CLAUDE_MAX_TIMEOUT", "120"))


def _find_claude_cli() -> Optional[str]:
    """Locate claude.exe / claude. Honors $CLAUDE_AGENT_CLI_PATH override."""
    override = os.environ.get("CLAUDE_AGENT_CLI_PATH", "")
    if override and Path(override).exists():
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # Windows install dir: AppData\Roaming\Claude\claude-code\<version>\claude.exe
    win_roots = [
        os.path.expandvars(r"%APPDATA%\Claude\claude-code"),
        os.path.expanduser(r"~\AppData\Roaming\Claude\claude-code"),
    ]
    candidates: list[str] = []
    for root in win_roots:
        if not os.path.isdir(root):
            continue
        candidates.extend(glob.glob(os.path.join(root, "*", "claude.exe")))
    if not candidates:
        return None
    # Pick the highest version directory (lexically newest is fine for SemVer-ish)
    return max(candidates)


class ClaudeAgentSDKProvider(Provider):
    name = "claude_max_sdk"

    def call(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        model_hint: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: int = 60,
        allowed_tools: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> InferenceResult:
        if images:
            raise ProviderError("SDK provider doesn't support images in v1")

        try:
            from claude_agent_sdk import (  # type: ignore[import-not-found]
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                query,
            )
        except ImportError as e:
            raise ProviderError("claude-agent-sdk not installed") from e

        run_timeout = max(timeout, DEFAULT_TIMEOUT)
        model = model_hint or DEFAULT_MODEL_HINT or None
        cli_path = _find_claude_cli()
        if not cli_path:
            raise ProviderError(
                "claude CLI not found — install Claude Code or set "
                "$CLAUDE_AGENT_CLI_PATH"
            )

        # Decide max_turns based on whether tools are allowed. With tools,
        # the agent may need multiple turns to read/grep/synthesize.
        effective_tools = list(allowed_tools or [])
        max_turns_value = 8 if effective_tools else 1

        async def _run() -> tuple[str, dict]:
            kwargs = {
                "model": model,
                "cli_path": cli_path,
                "allowed_tools": effective_tools,
                "max_turns": max_turns_value,
            }
            if cwd:
                kwargs["cwd"] = cwd
            options = ClaudeAgentOptions(**kwargs)
            text_parts: list[str] = []
            metadata: dict = {}
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        # TextBlock has .text; other block types ignored.
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                elif isinstance(message, ResultMessage):
                    metadata["stop_reason"] = getattr(message, "stop_reason", None)
                    metadata["session_id"] = getattr(message, "session_id", None)
                    usage = getattr(message, "usage", None)
                    if usage:
                        metadata["usage"] = usage
            return "".join(text_parts), metadata

        t0 = time.perf_counter()
        try:
            text, metadata = asyncio.run(asyncio.wait_for(_run(), timeout=run_timeout))
        except asyncio.TimeoutError as e:
            raise ProviderError(f"SDK query timed out after {run_timeout}s") from e
        except RuntimeError as e:
            # asyncio.run() refuses if a loop is already running.
            if "already running" in str(e).lower():
                raise ProviderError(
                    "SDK provider called from inside an event loop — "
                    "wrap in a thread or use ClaudeSDKClient"
                ) from e
            raise ProviderError(f"SDK runtime error: {e}") from e
        except Exception as e:
            raise ProviderError(f"SDK query failed: {type(e).__name__}: {e}") from e
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        text = text.strip()
        if not text:
            raise ProviderError("SDK query returned empty text")

        return InferenceResult(
            text=text,
            provider=self.name,
            model=model or metadata.get("model_used", "max-default"),
            input_tokens=int(metadata.get("usage", {}).get("input_tokens", 0) or 0)
            if isinstance(metadata.get("usage"), dict) else 0,
            output_tokens=int(metadata.get("usage", {}).get("output_tokens", 0) or 0)
            if isinstance(metadata.get("usage"), dict) else 0,
            cost_usd=0.0,  # drains Max plan, no $ marginal
            latency_ms=elapsed_ms,
            metadata={
                "stop_reason": metadata.get("stop_reason"),
                "session_id": metadata.get("session_id"),
            },
        )
