"""Storage for project next-steps cache.

Reuses utilization.db (same DB the rest of the dashboard writes to). Adds
one table — project_next_steps — keyed by (project_id, ts). Only the
latest row is shown in the UI; we keep history so we can diff over time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from utilization_dashboard.storage import DB_PATH  # reuse the same DB


_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_next_steps (
    project_id      TEXT NOT NULL,
    ts              TEXT NOT NULL,
    content         TEXT NOT NULL,         -- markdown bullets
    model_used      TEXT,                  -- e.g. "claude-sonnet-4-5"
    provider_used   TEXT,                  -- e.g. "claude_max_sdk"
    latency_ms      INTEGER,
    cost_usd        REAL,
    bundle_summary  TEXT,                  -- JSON: what was in the prompt
    PRIMARY KEY (project_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_pns_project_ts
    ON project_next_steps (project_id, ts DESC);
"""


def _init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(_SCHEMA)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write(
    project_id: str,
    *,
    content: str,
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    bundle_summary: Optional[dict] = None,
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO project_next_steps "
            "(project_id, ts, content, model_used, provider_used, latency_ms, "
            " cost_usd, bundle_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, ts, content, model_used, provider_used,
             latency_ms, cost_usd,
             json.dumps(bundle_summary) if bundle_summary else None),
        )
    return ts


def latest(project_id: str) -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM project_next_steps WHERE project_id=? "
            "ORDER BY ts DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("bundle_summary"):
        try:
            d["bundle_summary"] = json.loads(d["bundle_summary"])
        except json.JSONDecodeError:
            pass
    return d


def latest_all() -> dict[str, dict]:
    """Return {project_id: latest_row} for fast page rendering."""
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT pns.* FROM project_next_steps pns "
            "INNER JOIN ("
            "  SELECT project_id, MAX(ts) AS max_ts "
            "  FROM project_next_steps GROUP BY project_id"
            ") latest ON pns.project_id=latest.project_id "
            "        AND pns.ts=latest.max_ts"
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d.get("bundle_summary"):
            try:
                d["bundle_summary"] = json.loads(d["bundle_summary"])
            except json.JSONDecodeError:
                pass
        out[d["project_id"]] = d
    return out
