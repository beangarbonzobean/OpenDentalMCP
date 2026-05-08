"""Background-thread runner for long-running agent dispatches.

Routes that fire L1/L2/L3 agents take 30s-3min. Without backgrounding,
the user's browser hangs on the POST and they can't do anything else.

This module gives those routes a fire-and-forget pattern:
  1. Caller writes a 'running' row to the appropriate table
  2. Caller submits the actual work to this runner
  3. Caller returns task_id to the user immediately
  4. Frontend polls the snapshot endpoint until the row's status changes
  5. Runner thread updates the row to 'ok' / 'failed' when done

Each Claude Agent SDK call uses asyncio.run() internally, which creates
its own event loop — running multiple in parallel threads is supported
by asyncio. We cap concurrency at 4 to avoid over-saturating the user's
Claude Max session quota.

Use cases tonight:
  - Manager bullet actions (plan / investigate)
  - Manager-originated Draft PoC (L2 against a chosen project)
  - Per-project investigate / propose / refresh-next-steps

Existing fully-synchronous routes still work; this is opt-in per route.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Callable, Optional

log = logging.getLogger(__name__)

_MAX_WORKERS = int(os.environ.get("DASHBOARD_AGENT_WORKERS", "4"))
_executor: Optional[ThreadPoolExecutor] = None
_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS,
                thread_name_prefix="agent-task",
            )
        return _executor


def submit(fn: Callable, *args, **kwargs) -> Future:
    """Submit a callable to the shared agent-task pool.

    The callable is responsible for updating its own status row (typically
    via projects_storage helpers). This wrapper just adds basic error
    logging so a thread failure doesn't disappear silently.
    """
    def _wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.exception("agent-task failed: %s", e)
            raise
    return _get_executor().submit(_wrapped, *args, **kwargs)


def shutdown(wait: bool = False) -> None:
    """Tear down the executor (for tests / clean process exit)."""
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=not wait)
            _executor = None
