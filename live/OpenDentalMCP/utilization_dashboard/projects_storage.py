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

CREATE TABLE IF NOT EXISTS user_idea (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    idea            TEXT NOT NULL,           -- raw user input
    status          TEXT NOT NULL,           -- 'processing' | 'ok' | 'failed'
    section         TEXT,                    -- which manager section
    bullet          TEXT,                    -- Opus-produced polished bullet
    bullet_hash     TEXT,                    -- sha1 of bullet text (for action keying)
    model_used      TEXT,
    provider_used   TEXT,
    latency_ms      INTEGER,
    cost_usd        REAL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_idea_ts ON user_idea(ts DESC);

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

CREATE TABLE IF NOT EXISTS proposal_verification (
    project_id      TEXT NOT NULL,
    bullet_hash     TEXT NOT NULL,
    proposal_ts     TEXT NOT NULL,         -- joins to project_proposal.ts
    ts              TEXT NOT NULL,         -- when the verifier ran
    tier            TEXT NOT NULL,         -- v1_smoke | v2_unit | v3_service | v4_ui | v5_schema
    status          TEXT NOT NULL,         -- pass | fail | skip | block
    summary         TEXT,                  -- one-line badge text
    evidence        TEXT,                  -- JSON blob with structured detail
    latency_ms      INTEGER,
    blocked_apply   INTEGER NOT NULL DEFAULT 0,  -- 0/1 — was L3 auto-merge blocked
    PRIMARY KEY (project_id, bullet_hash, proposal_ts)
);
CREATE INDEX IF NOT EXISTS idx_pv_project_bullet
    ON proposal_verification (project_id, bullet_hash, ts DESC);
"""


def _init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(_SCHEMA)
        # Idempotent column adds for tables that gained columns post-creation.
        # SQLite errors if the column already exists; we swallow that case.
        for sql in (
            "ALTER TABLE manager_brief ADD COLUMN prompt TEXT",
        ):
            try:
                db.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists


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


def write_user_idea(idea: str, *, status: str = "processing") -> int:
    _init()
    ts = _now_iso()
    with _conn() as db:
        cur = db.execute(
            "INSERT INTO user_idea (ts, idea, status) VALUES (?, ?, ?)",
            (ts, idea, status),
        )
        return cur.lastrowid


def update_user_idea(
    idea_id: int,
    *,
    status: str,
    section: str = "",
    bullet: str = "",
    bullet_hash: str = "",
    model_used: str = "",
    provider_used: str = "",
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    error: str = "",
) -> None:
    _init()
    with _conn() as db:
        db.execute(
            "UPDATE user_idea SET status=?, section=?, bullet=?, bullet_hash=?, "
            " model_used=?, provider_used=?, latency_ms=?, cost_usd=?, error=? "
            "WHERE id=?",
            (status, section, bullet, bullet_hash, model_used, provider_used,
             latency_ms, cost_usd, error, idea_id),
        )


def list_user_ideas(limit: int = 50) -> list[dict]:
    """Return user-submitted ideas, newest first. Successful ones expose the
    Opus-produced bullet and section so the UI can fold them into the brief.

    Excludes rows the user dismissed (status='dismissed') so an old failed
    submission doesn't permanently haunt the dashboard.
    """
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM user_idea "
            "WHERE status != 'dismissed' "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def dismiss_user_idea(idea_id: int) -> bool:
    """Mark a user_idea row as dismissed so list_user_ideas hides it.
    Returns True if a row was updated.
    """
    _init()
    with _conn() as db:
        cur = db.execute(
            "UPDATE user_idea SET status='dismissed' WHERE id=? AND status != 'dismissed'",
            (idea_id,),
        )
        return cur.rowcount > 0


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


def manager_proposals_by_bullet() -> dict:
    """Return manager-originated L2 proposals grouped by bullet_hash.

    Shape: {bullet_hash: {project_id: latest_proposal_row, ...}}
    """
    _init()
    out: dict[str, dict[str, dict]] = {}
    with _conn() as db:
        rows = db.execute(
            "SELECT pp.* FROM project_proposal pp "
            "INNER JOIN ("
            "  SELECT project_id, bullet_hash, MAX(ts) AS max_ts "
            "  FROM project_proposal WHERE mode='manager-l2' "
            "  GROUP BY project_id, bullet_hash"
            ") latest "
            "  ON pp.project_id=latest.project_id "
            " AND pp.bullet_hash=latest.bullet_hash "
            " AND pp.ts=latest.max_ts "
            "WHERE pp.mode='manager-l2'"
        ).fetchall()
    for r in rows:
        d = dict(r)
        if d.get("files_changed"):
            try:
                d["files_changed"] = json.loads(d["files_changed"])
            except json.JSONDecodeError:
                pass
        out.setdefault(d["bullet_hash"], {})[d["project_id"]] = d
    return out


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
    prompt: str = "",
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO manager_brief "
            "(ts, content, model_used, provider_used, latency_ms, cost_usd, "
            " projects_in_bundle, bundle_chars, prompt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, content, model_used, provider_used, latency_ms, cost_usd,
             json.dumps(projects_in_bundle or []), bundle_chars, prompt),
        )
    return ts


def latest_manager_brief(*, include_prompt: bool = False) -> Optional[dict]:
    """Return the most recent manager brief, optionally including the
    full prompt that produced it (for the 'show prompt' UI footer).

    The prompt is excluded by default because briefs are large (10-20k chars)
    and the polling endpoint runs every ~30s.
    """
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
    if not include_prompt:
        d.pop("prompt", None)
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


# ---------------------------------------------------------------------------
# proposal_verification — written by verifier.py before L3 merges
# ---------------------------------------------------------------------------


def write_verification(
    project_id: str,
    bullet_hash: str,
    proposal_ts: str,
    *,
    tier: str,
    status: str,
    summary: str = "",
    evidence: Optional[dict] = None,
    latency_ms: int = 0,
    blocked_apply: bool = False,
) -> str:
    _init()
    ts = _now_iso()
    with _conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO proposal_verification "
            "(project_id, bullet_hash, proposal_ts, ts, tier, status, "
            " summary, evidence, latency_ms, blocked_apply) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, bullet_hash, proposal_ts, ts, tier, status,
             summary, json.dumps(evidence or {}), latency_ms,
             1 if blocked_apply else 0),
        )
    return ts


def latest_verification(
    project_id: str, bullet_hash: str, proposal_ts: Optional[str] = None,
) -> Optional[dict]:
    """Most recent verification row for a (project, bullet) — optionally
    pinned to a specific proposal_ts."""
    _init()
    with _conn() as db:
        if proposal_ts is not None:
            row = db.execute(
                "SELECT * FROM proposal_verification "
                "WHERE project_id=? AND bullet_hash=? AND proposal_ts=? "
                "ORDER BY ts DESC LIMIT 1",
                (project_id, bullet_hash, proposal_ts),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM proposal_verification "
                "WHERE project_id=? AND bullet_hash=? "
                "ORDER BY ts DESC LIMIT 1",
                (project_id, bullet_hash),
            ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("evidence"):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except json.JSONDecodeError:
            pass
    d["blocked_apply"] = bool(d.get("blocked_apply"))
    return d


def verifications_for_project(project_id: str) -> dict[str, dict]:
    """Return {bullet_hash: latest_verification_row} for the project."""
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT pv.* FROM proposal_verification pv "
            "INNER JOIN ("
            "  SELECT project_id, bullet_hash, MAX(ts) AS max_ts "
            "  FROM proposal_verification WHERE project_id=? "
            "  GROUP BY project_id, bullet_hash"
            ") latest "
            "  ON pv.project_id=latest.project_id "
            " AND pv.bullet_hash=latest.bullet_hash "
            " AND pv.ts=latest.max_ts",
            (project_id,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d.get("evidence"):
            try:
                d["evidence"] = json.loads(d["evidence"])
            except json.JSONDecodeError:
                pass
        d["blocked_apply"] = bool(d.get("blocked_apply"))
        out[d["bullet_hash"]] = d
    return out
