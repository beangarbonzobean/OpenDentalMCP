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

CREATE TABLE IF NOT EXISTS project_investigation (
    project_id      TEXT NOT NULL,
    bullet_hash     TEXT NOT NULL,         -- sha1 of bullet text, deduplication key
    ts              TEXT NOT NULL,
    bullet_text     TEXT NOT NULL,
    content         TEXT NOT NULL,         -- markdown report from agent
    model_used      TEXT,
    provider_used   TEXT,
    latency_ms      INTEGER,
    cost_usd        REAL,
    tools_used      TEXT,                  -- JSON list of tool names
    cwd             TEXT,
    PRIMARY KEY (project_id, bullet_hash, ts)
);
CREATE INDEX IF NOT EXISTS idx_pi_project_bullet
    ON project_investigation (project_id, bullet_hash, ts DESC);

CREATE TABLE IF NOT EXISTS manager_action (
    bullet_hash    TEXT NOT NULL,           -- sha1 of bullet text, 16 chars
    action         TEXT NOT NULL,           -- 'plan' | 'investigate' | ...
    ts             TEXT NOT NULL,
    bullet_text    TEXT NOT NULL,
    section        TEXT,                    -- which manager section (opportunity/pattern/recommendation)
    status         TEXT NOT NULL,           -- 'running' | 'ok' | 'failed'
    content        TEXT,                    -- markdown report
    model_used     TEXT,
    provider_used  TEXT,
    latency_ms     INTEGER,
    cost_usd       REAL,
    error          TEXT,
    PRIMARY KEY (bullet_hash, action, ts)
);
CREATE INDEX IF NOT EXISTS idx_manager_action
    ON manager_action (bullet_hash, action, ts DESC);

CREATE TABLE IF NOT EXISTS manager_brief (
    ts                 TEXT PRIMARY KEY,
    content            TEXT NOT NULL,
    model_used         TEXT,
    provider_used      TEXT,
    latency_ms         INTEGER,
    cost_usd           REAL,
    projects_in_bundle TEXT,                -- JSON list of project ids
    bundle_chars       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_manager_brief_ts ON manager_brief(ts DESC);

CREATE TABLE IF NOT EXISTS project_proposal (
    project_id      TEXT NOT NULL,
    bullet_hash     TEXT NOT NULL,
    ts              TEXT NOT NULL,
    bullet_text     TEXT NOT NULL,
    mode            TEXT NOT NULL,         -- 'l2' (review) | 'l3' (auto-apply)
    status          TEXT NOT NULL,         -- 'pending' | 'applied' | 'discarded' | 'failed'
    summary         TEXT,                  -- agent's markdown summary of changes
    diff            TEXT,                  -- unified diff vs. main
    files_changed   TEXT,                  -- JSON list of paths
    worktree_path   TEXT,                  -- absolute path; cleared on apply/discard
    branch          TEXT,
    model_used      TEXT,
    provider_used   TEXT,
    latency_ms      INTEGER,
    cost_usd        REAL,
    error           TEXT,
    PRIMARY KEY (project_id, bullet_hash, ts)
);
CREATE INDEX IF NOT EXISTS idx_pp_project_bullet
    ON project_proposal (project_id, bullet_hash, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pp_status ON project_proposal (status);
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


def write_investigation(
    project_id: str,
    bullet_hash: str,
    bullet_text: str,
    *,
    content: str,
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    tools_used: Optional[list[str]] = None,
    cwd: str = "",
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO project_investigation "
            "(project_id, bullet_hash, ts, bullet_text, content, "
            " model_used, provider_used, latency_ms, cost_usd, tools_used, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, bullet_hash, ts, bullet_text, content,
             model_used, provider_used, latency_ms, cost_usd,
             json.dumps(tools_used) if tools_used else None, cwd),
        )
    return ts


def latest_investigation(project_id: str, bullet_hash: str) -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM project_investigation "
            "WHERE project_id=? AND bullet_hash=? "
            "ORDER BY ts DESC LIMIT 1",
            (project_id, bullet_hash),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("tools_used"):
        try:
            d["tools_used"] = json.loads(d["tools_used"])
        except json.JSONDecodeError:
            pass
    return d


def investigations_for_project(project_id: str) -> dict[str, dict]:
    """Return {bullet_hash: latest_investigation_row} for a project."""
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT pi.* FROM project_investigation pi "
            "INNER JOIN ("
            "  SELECT project_id, bullet_hash, MAX(ts) AS max_ts "
            "  FROM project_investigation WHERE project_id=? "
            "  GROUP BY project_id, bullet_hash"
            ") latest "
            "  ON pi.project_id=latest.project_id "
            " AND pi.bullet_hash=latest.bullet_hash "
            " AND pi.ts=latest.max_ts",
            (project_id,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d.get("tools_used"):
            try:
                d["tools_used"] = json.loads(d["tools_used"])
            except json.JSONDecodeError:
                pass
        out[d["bullet_hash"]] = d
    return out


def write_manager_action(
    bullet_hash: str,
    action: str,
    bullet_text: str,
    *,
    section: str = "",
    status: str = "running",
    content: str = "",
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    error: str = "",
    ts: Optional[str] = None,
) -> str:
    _init()
    ts = ts or _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO manager_action "
            "(bullet_hash, action, ts, bullet_text, section, status, content, "
            " model_used, provider_used, latency_ms, cost_usd, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bullet_hash, action, ts, bullet_text, section, status, content,
             model_used, provider_used, latency_ms, cost_usd, error),
        )
    return ts


def latest_manager_action(bullet_hash: str, action: str) -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM manager_action "
            "WHERE bullet_hash=? AND action=? ORDER BY ts DESC LIMIT 1",
            (bullet_hash, action),
        ).fetchone()
    return dict(row) if row else None


def all_manager_actions() -> dict:
    """Return {bullet_hash: {action: latest_row}} for the manager UI."""
    _init()
    out: dict[str, dict[str, dict]] = {}
    with _conn() as db:
        rows = db.execute(
            "SELECT ma.* FROM manager_action ma "
            "INNER JOIN ("
            "  SELECT bullet_hash, action, MAX(ts) AS max_ts "
            "  FROM manager_action GROUP BY bullet_hash, action"
            ") latest "
            "  ON ma.bullet_hash=latest.bullet_hash "
            " AND ma.action=latest.action "
            " AND ma.ts=latest.max_ts"
        ).fetchall()
    for r in rows:
        d = dict(r)
        out.setdefault(d["bullet_hash"], {})[d["action"]] = d
    return out


def write_manager_brief(
    *,
    content: str,
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    projects_in_bundle: Optional[list[str]] = None,
    bundle_chars: int = 0,
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO manager_brief "
            "(ts, content, model_used, provider_used, latency_ms, cost_usd, "
            " projects_in_bundle, bundle_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, content, model_used, provider_used, latency_ms, cost_usd,
             json.dumps(projects_in_bundle or []), bundle_chars),
        )
    return ts


def latest_manager_brief() -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM manager_brief ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("projects_in_bundle"):
        try:
            d["projects_in_bundle"] = json.loads(d["projects_in_bundle"])
        except json.JSONDecodeError:
            pass
    return d


def write_proposal(
    project_id: str,
    bullet_hash: str,
    bullet_text: str,
    *,
    mode: str,
    status: str,
    summary: str = "",
    diff: str = "",
    files_changed: Optional[list[str]] = None,
    worktree_path: str = "",
    branch: str = "",
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    error: str = "",
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO project_proposal "
            "(project_id, bullet_hash, ts, bullet_text, mode, status, summary, "
            " diff, files_changed, worktree_path, branch, model_used, "
            " provider_used, latency_ms, cost_usd, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, bullet_hash, ts, bullet_text, mode, status, summary,
             diff, json.dumps(files_changed or []), worktree_path, branch,
             model_used, provider_used, latency_ms, cost_usd, error),
        )
    return ts


def update_proposal_status(
    project_id: str, bullet_hash: str, ts: str,
    *, status: str, error: str = "", clear_worktree: bool = False,
) -> None:
    _init()
    sets = ["status=?"]
    args: list = [status]
    if error:
        sets.append("error=?")
        args.append(error)
    if clear_worktree:
        sets.append("worktree_path=''")
    args += [project_id, bullet_hash, ts]
    with _conn() as db:
        db.execute(
            f"UPDATE project_proposal SET {', '.join(sets)} "
            "WHERE project_id=? AND bullet_hash=? AND ts=?",
            args,
        )


def latest_proposal(project_id: str, bullet_hash: str) -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM project_proposal "
            "WHERE project_id=? AND bullet_hash=? "
            "ORDER BY ts DESC LIMIT 1",
            (project_id, bullet_hash),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("files_changed"):
        try:
            d["files_changed"] = json.loads(d["files_changed"])
        except json.JSONDecodeError:
            pass
    return d


def proposals_for_project(project_id: str) -> dict[str, dict]:
    """Return {bullet_hash: latest_proposal} for a project."""
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT pp.* FROM project_proposal pp "
            "INNER JOIN ("
            "  SELECT project_id, bullet_hash, MAX(ts) AS max_ts "
            "  FROM project_proposal WHERE project_id=? "
            "  GROUP BY project_id, bullet_hash"
            ") latest "
            "  ON pp.project_id=latest.project_id "
            " AND pp.bullet_hash=latest.bullet_hash "
            " AND pp.ts=latest.max_ts",
            (project_id,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d.get("files_changed"):
            try:
                d["files_changed"] = json.loads(d["files_changed"])
            except json.JSONDecodeError:
                pass
        out[d["bullet_hash"]] = d
    return out


def get_proposal_by_ts(project_id: str, bullet_hash: str, ts: str) -> Optional[dict]:
    _init()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM project_proposal "
            "WHERE project_id=? AND bullet_hash=? AND ts=?",
            (project_id, bullet_hash, ts),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("files_changed"):
        try:
            d["files_changed"] = json.loads(d["files_changed"])
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
