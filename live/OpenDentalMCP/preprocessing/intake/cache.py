"""
SQLite cache for the intake pipeline.

Two tables:

- intake_pending: rows for filing-candidate documents extracted from a batch
  scan. Each row represents ONE candidate document (which may span multiple
  pages of the source PDF). State machine:

    pending -> queued (awaiting staff review)
    pending -> auto_filed (confidence >= threshold, written to OD)
    queued  -> filed     (staff confirmed)
    queued  -> rejected  (staff rejected; file moved to quarantine)
    queued  -> overridden (staff fixed patient/category, then filed)

- intake_audit: append-only log of every action that affected OD or the
  file system. Joined on intake_pending.id when applicable.

Both tables live in live/OpenDentalMCP/data/intake.db, separate from the
document-text cache so the schemas can evolve independently.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


DEFAULT_INTAKE_DB = Path(__file__).resolve().parent.parent.parent / "data" / "intake.db"


VALID_STATUS = {
    "pending",        # extracted, not yet decided
    "queued",         # awaiting staff confirmation
    "auto_filed",     # auto-filed without staff intervention
    "filed",          # staff-confirmed and filed
    "overridden",     # staff fixed and filed
    "rejected",       # staff rejected; file moved to quarantine
    "error",          # processing failed; needs investigation
}


# Each row represents one candidate-document (one or more contiguous source-PDF
# pages identified by `page_indices` JSON array, e.g., "[3, 4, 5]").
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intake_pending (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf          TEXT NOT NULL,
    source_pdf_sha256   TEXT NOT NULL,
    page_indices        TEXT NOT NULL,              -- JSON array of int page indexes
    extracted_name      TEXT,
    extracted_dob       TEXT,
    extracted_text_len  INTEGER,
    suggested_pat_num   INTEGER,
    suggested_pat_label TEXT,                       -- 'Lastname, Firstname (PatNum)'
    suggested_category  TEXT,                       -- short_label from taxonomy
    suggested_def_num   INTEGER,                    -- OD DocCategory DefNum
    patient_confidence  REAL,                       -- 0.0 - 1.0
    category_confidence REAL,                       -- 0.0 - 1.0
    split_confidence    REAL,                       -- 0.0 - 1.0
    overall_confidence  REAL,                       -- product or min of the above
    status              TEXT NOT NULL DEFAULT 'pending',
    target_doc_num      INTEGER,                    -- OD DocNum after filing
    target_file_path    TEXT,                       -- final path on the share
    error_message       TEXT,
    discovered_at       TEXT NOT NULL,
    decided_at          TEXT,
    decided_by          TEXT                        -- 'auto-file' or staff name
);
CREATE INDEX IF NOT EXISTS idx_intake_pending_status   ON intake_pending(status);
CREATE INDEX IF NOT EXISTS idx_intake_pending_pat      ON intake_pending(suggested_pat_num);
CREATE INDEX IF NOT EXISTS idx_intake_pending_source   ON intake_pending(source_pdf_sha256);

CREATE TABLE IF NOT EXISTS intake_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_id      INTEGER,
    action          TEXT NOT NULL,    -- 'extracted','queued','auto_filed','filed','overridden','rejected','unfiled','error'
    actor           TEXT NOT NULL,    -- 'system','auto-file','staff:<name>', etc.
    details         TEXT,             -- JSON blob: pat_num, def_num, file_path, confidences, etc.
    occurred_at     TEXT NOT NULL,
    FOREIGN KEY (pending_id) REFERENCES intake_pending(id)
);
CREATE INDEX IF NOT EXISTS idx_intake_audit_pending ON intake_audit(pending_id);
CREATE INDEX IF NOT EXISTS idx_intake_audit_action  ON intake_audit(action);

CREATE TABLE IF NOT EXISTS intake_processed_pdfs (
    sha256          TEXT PRIMARY KEY,
    source_pdf      TEXT NOT NULL,
    page_count      INTEGER,
    candidates      INTEGER,
    processed_at    TEXT NOT NULL
);
"""


@dataclass
class IntakePending:
    id: Optional[int] = None
    source_pdf: str = ""
    source_pdf_sha256: str = ""
    page_indices: list[int] = field(default_factory=list)
    extracted_name: Optional[str] = None
    extracted_dob: Optional[str] = None
    extracted_text_len: Optional[int] = None
    suggested_pat_num: Optional[int] = None
    suggested_pat_label: Optional[str] = None
    suggested_category: Optional[str] = None
    suggested_def_num: Optional[int] = None
    patient_confidence: Optional[float] = None
    category_confidence: Optional[float] = None
    split_confidence: Optional[float] = None
    overall_confidence: Optional[float] = None
    status: str = "pending"
    target_doc_num: Optional[int] = None
    target_file_path: Optional[str] = None
    error_message: Optional[str] = None
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None


@dataclass
class IntakeAudit:
    pending_id: Optional[int]
    action: str
    actor: str
    details: dict
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_cache(path: Optional[Path] = None) -> Path:
    p = Path(path) if path is not None else DEFAULT_INTAKE_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(p)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return p


@contextmanager
def open_cache(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = init_cache(path)
    conn = _connect(p)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# intake_pending operations
# ---------------------------------------------------------------------------

def insert_pending(conn: sqlite3.Connection, row: IntakePending) -> int:
    if row.status not in VALID_STATUS:
        raise ValueError(f"invalid status: {row.status!r}")
    cur = conn.execute(
        """
        INSERT INTO intake_pending (
            source_pdf, source_pdf_sha256, page_indices,
            extracted_name, extracted_dob, extracted_text_len,
            suggested_pat_num, suggested_pat_label,
            suggested_category, suggested_def_num,
            patient_confidence, category_confidence,
            split_confidence, overall_confidence,
            status, target_doc_num, target_file_path, error_message,
            discovered_at, decided_at, decided_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.source_pdf, row.source_pdf_sha256, json.dumps(row.page_indices),
            row.extracted_name, row.extracted_dob, row.extracted_text_len,
            row.suggested_pat_num, row.suggested_pat_label,
            row.suggested_category, row.suggested_def_num,
            row.patient_confidence, row.category_confidence,
            row.split_confidence, row.overall_confidence,
            row.status, row.target_doc_num, row.target_file_path, row.error_message,
            row.discovered_at, row.decided_at, row.decided_by,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_pending(conn: sqlite3.Connection, pending_id: int) -> Optional[IntakePending]:
    cur = conn.execute("SELECT * FROM intake_pending WHERE id = ?", (int(pending_id),))
    r = cur.fetchone()
    return _row_to_pending(r) if r else None


def list_by_status(conn: sqlite3.Connection, status: str, limit: int = 200) -> list[IntakePending]:
    cur = conn.execute(
        "SELECT * FROM intake_pending WHERE status = ? ORDER BY discovered_at DESC LIMIT ?",
        (status, int(limit)),
    )
    return [_row_to_pending(r) for r in cur.fetchall()]


def update_pending_status(
    conn: sqlite3.Connection,
    pending_id: int,
    *,
    status: str,
    target_doc_num: Optional[int] = None,
    target_file_path: Optional[str] = None,
    error_message: Optional[str] = None,
    decided_by: Optional[str] = None,
    suggested_pat_num: Optional[int] = None,
    suggested_def_num: Optional[int] = None,
) -> None:
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status!r}")
    fields: list[str] = ["status = ?"]
    params: list[Any] = [status]
    if status not in ("pending",):
        fields.append("decided_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
    if target_doc_num is not None:
        fields.append("target_doc_num = ?")
        params.append(int(target_doc_num))
    if target_file_path is not None:
        fields.append("target_file_path = ?")
        params.append(str(target_file_path))
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    if decided_by is not None:
        fields.append("decided_by = ?")
        params.append(decided_by)
    if suggested_pat_num is not None:
        fields.append("suggested_pat_num = ?")
        params.append(int(suggested_pat_num))
    if suggested_def_num is not None:
        fields.append("suggested_def_num = ?")
        params.append(int(suggested_def_num))
    params.append(int(pending_id))
    conn.execute(
        f"UPDATE intake_pending SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# intake_audit operations
# ---------------------------------------------------------------------------

def write_audit(conn: sqlite3.Connection, audit: IntakeAudit) -> int:
    cur = conn.execute(
        """
        INSERT INTO intake_audit (pending_id, action, actor, details, occurred_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            audit.pending_id,
            audit.action,
            audit.actor,
            json.dumps(audit.details, default=str),
            audit.occurred_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_audit_for_pending(conn: sqlite3.Connection, pending_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM intake_audit WHERE pending_id = ? ORDER BY occurred_at",
        (int(pending_id),),
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "id": int(r["id"]),
            "pending_id": r["pending_id"],
            "action": r["action"],
            "actor": r["actor"],
            "details": json.loads(r["details"]) if r["details"] else {},
            "occurred_at": r["occurred_at"],
        })
    return out


# ---------------------------------------------------------------------------
# intake_processed_pdfs (so we don't re-process the same batch)
# ---------------------------------------------------------------------------

def is_pdf_processed(conn: sqlite3.Connection, sha256: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM intake_processed_pdfs WHERE sha256 = ?",
        (sha256,),
    )
    return cur.fetchone() is not None


def mark_pdf_processed(
    conn: sqlite3.Connection,
    sha256: str,
    source_pdf: str,
    page_count: int,
    candidates: int,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO intake_processed_pdfs
            (sha256, source_pdf, page_count, candidates, processed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (sha256, source_pdf, int(page_count), int(candidates),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _row_to_pending(r: sqlite3.Row) -> IntakePending:
    pages_raw = r["page_indices"]
    pages = json.loads(pages_raw) if pages_raw else []
    return IntakePending(
        id=int(r["id"]),
        source_pdf=r["source_pdf"],
        source_pdf_sha256=r["source_pdf_sha256"],
        page_indices=list(pages),
        extracted_name=r["extracted_name"],
        extracted_dob=r["extracted_dob"],
        extracted_text_len=r["extracted_text_len"],
        suggested_pat_num=r["suggested_pat_num"],
        suggested_pat_label=r["suggested_pat_label"],
        suggested_category=r["suggested_category"],
        suggested_def_num=r["suggested_def_num"],
        patient_confidence=r["patient_confidence"],
        category_confidence=r["category_confidence"],
        split_confidence=r["split_confidence"],
        overall_confidence=r["overall_confidence"],
        status=r["status"],
        target_doc_num=r["target_doc_num"],
        target_file_path=r["target_file_path"],
        error_message=r["error_message"],
        discovered_at=r["discovered_at"],
        decided_at=r["decided_at"],
        decided_by=r["decided_by"],
    )
