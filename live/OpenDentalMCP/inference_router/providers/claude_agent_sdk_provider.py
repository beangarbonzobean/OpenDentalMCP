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


def _build_write_scope_callback(scope_dir: str):
    """Returns an async can_use_tool callback that allows Write/Edit only
    when the target path resolves inside scope_dir.

    Read/Grep/Glob always pass through. Anything we don't recognize is
    denied with a useful message.
    """
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        PermissionResultAllow,
        PermissionResultDeny,
    )
    scope = Path(scope_dir).resolve()

    def _resolve_input_path(tool_input: dict) -> Optional[Path]:
        # Both Write and Edit use a "file_path" key per Claude Code conventions.
        raw = tool_input.get("file_path") or tool_input.get("path")
        if not raw:
            return None
        try:
            return Path(raw).resolve()
        except OSError:
            return None

    def _is_inside(target: Path, root: Path) -> bool:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            return False

    async def can_use_tool(tool_name, tool_input, context):
        # Read-only and search tools: always allow.
        if tool_name in ("Read", "Grep", "Glob"):
            return PermissionResultAllow()
        # Write/Edit: must target a path inside scope.
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            target = _resolve_input_path(tool_input or {})
            if target is None:
                return PermissionResultDeny(
                    message=f"{tool_name} call missing file_path",
                    interrupt=False,
                )
            if not _is_inside(target, scope):
                return PermissionResultDeny(
                    message=(
                        f"{tool_name} target {target} is outside the allowed "
                        f"write scope {scope}. Refused."
                    ),
                    interrupt=False,
                )
            return PermissionResultAllow()
        # Anything else (Bash, web, network, MCP tools) — deny by default.
        return PermissionResultDeny(
            message=f"tool {tool_name!r} is not enabled in this scope",
            interrupt=False,
        )

    return can_use_tool


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
        write_scope: Optional[str] = None,
    ) -> InferenceResult:
        """write_scope: when Write/Edit are in allowed_tools, refuse any
        write whose path resolves outside this directory. Pass the
        project's repo root as write_scope to keep agent edits scoped."""
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
        # the agent may need multiple turns to read/grep/synthesize. Edit
        # workflows need more turns than pure investigation.
        effective_tools = list(allowed_tools or [])
        write_tools_present = any(t in effective_tools for t in ("Write", "Edit"))
        if not effective_tools:
            max_turns_value = 1
        elif write_tools_present:
            max_turns_value = 20
        else:
            max_turns_value = 8

        # Always disallow Bash from this provider — too broad for safe
        # autonomous use. (L4 / shell mode would be a separate provider
        # with its own command allowlist.)
        disallowed = ["Bash"]

        # Build the write-scope guard if write tools are enabled.
        permission_callback = None
        if write_tools_present and write_scope:
            permission_callback = _build_write_scope_callback(write_scope)

        async def _streaming_prompt():
            """When can_use_tool is set the SDK requires AsyncIterable mode."""
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }

        async def _run() -> tuple[str, dict]:
            kwargs = {
                "model": model,
                "cli_path": cli_path,
                "allowed_tools": effective_tools,
                "disallowed_tools": disallowed,
                "max_turns": max_turns_value,
            }
            if cwd:
                kwargs["cwd"] = cwd
            if permission_callback is not None:
                kwargs["can_use_tool"] = permission_callback
            options = ClaudeAgentOptions(**kwargs)
            text_parts: list[str] = []
            metadata: dict = {}
            # Streaming-mode prompt is required when can_use_tool is set;
            # safe to use it always.
            prompt_arg = _streaming_prompt() if permission_callback else prompt
            async for message in query(prompt=prompt_arg, options=options):
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
