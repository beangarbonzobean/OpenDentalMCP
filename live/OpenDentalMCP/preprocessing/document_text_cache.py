"""
SQLite cache for OCR'd Open Dental document text.

Storage: live/OpenDentalMCP/data/document_text_cache.db, opened in WAL mode so
the rebuild can write while ad-hoc reads happen concurrently.

Schema:
    doc_text(DocNum PK, PatNum, FileName, DocCategory, DateCreated, Text,
             PageCount, Sha256, OcrModel, OcrAt, Status, ErrorMessage, CostUsd)
    doc_text_fts — FTS5 virtual table over Text, content='doc_text', rowid=DocNum.

Status values:
    ok          — OCR succeeded with text
    unreadable  — Claude returned UNREADABLE (image too poor, blank, etc.)
    unsupported — file extension / size / category not OCR'd; never retry
    error       — transient or persistent failure; eligible for retry on rebuild
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional


DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "document_text_cache.db"


VALID_STATUS = {"ok", "unreadable", "unsupported", "error"}


@dataclass
class DocTextRow:
    DocNum: int
    PatNum: int
    FileName: Optional[str] = None
    DocCategory: Optional[int] = None
    DateCreated: Optional[str] = None
    Text: str = ""
    PageCount: Optional[int] = None
    Sha256: Optional[str] = None
    OcrModel: Optional[str] = None
    OcrAt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    Status: str = "ok"
    ErrorMessage: Optional[str] = None
    CostUsd: Optional[float] = None


@dataclass
class SearchHit:
    DocNum: int
    PatNum: int
    FileName: Optional[str]
    Snippet: str
    Score: float
    Status: str


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS doc_text (
    DocNum        INTEGER PRIMARY KEY,
    PatNum        INTEGER NOT NULL,
    FileName      TEXT,
    DocCategory   INTEGER,
    DateCreated   TEXT,
    Text          TEXT NOT NULL DEFAULT '',
    PageCount     INTEGER,
    Sha256        TEXT,
    OcrModel      TEXT,
    OcrAt         TEXT NOT NULL,
    Status        TEXT NOT NULL DEFAULT 'ok',
    ErrorMessage  TEXT,
    CostUsd       REAL
);
CREATE INDEX IF NOT EXISTS idx_doc_text_pat ON doc_text(PatNum);

CREATE VIRTUAL TABLE IF NOT EXISTS doc_text_fts USING fts5(
    Text,
    content='doc_text',
    content_rowid='DocNum',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS doc_text_ai AFTER INSERT ON doc_text BEGIN
    INSERT INTO doc_text_fts(rowid, Text) VALUES (new.DocNum, new.Text);
END;
CREATE TRIGGER IF NOT EXISTS doc_text_ad AFTER DELETE ON doc_text BEGIN
    INSERT INTO doc_text_fts(doc_text_fts, rowid, Text) VALUES ('delete', old.DocNum, old.Text);
END;
CREATE TRIGGER IF NOT EXISTS doc_text_au AFTER UPDATE ON doc_text BEGIN
    INSERT INTO doc_text_fts(doc_text_fts, rowid, Text) VALUES ('delete', old.DocNum, old.Text);
    INSERT INTO doc_text_fts(rowid, Text) VALUES (new.DocNum, new.Text);
END;
"""


_init_lock = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    # check_same_thread=False because the backfill runs from a thread pool.
    # All writes go through put_text which serializes via SQLite's own locking.
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_cache(path: Optional[Path] = None) -> Path:
    """Create the cache file and schema if missing. Returns the path used."""
    p = Path(path) if path is not None else DEFAULT_CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        conn = _connect(p)
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()
    return p


@contextmanager
def open_cache(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a connection. Caller does not need to commit
    for SELECTs; writes commit explicitly inside helpers."""
    p = init_cache(path)
    conn = _connect(p)
    try:
        yield conn
    finally:
        conn.close()


def get_text(conn: sqlite3.Connection, doc_num: int) -> Optional[DocTextRow]:
    cur = conn.execute(
        "SELECT * FROM doc_text WHERE DocNum = ?",
        (int(doc_num),),
    )
    r = cur.fetchone()
    if not r:
        return None
    return _row_to_dataclass(r)


def get_texts_for_patient(
    conn: sqlite3.Connection,
    pat_num: int,
    doc_category: Optional[int] = None,
) -> List[DocTextRow]:
    if doc_category is None:
        cur = conn.execute(
            "SELECT * FROM doc_text WHERE PatNum = ? ORDER BY DateCreated DESC, DocNum DESC",
            (int(pat_num),),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM doc_text WHERE PatNum = ? AND DocCategory = ? "
            "ORDER BY DateCreated DESC, DocNum DESC",
            (int(pat_num), int(doc_category)),
        )
    return [_row_to_dataclass(r) for r in cur.fetchall()]


def cached_doc_nums(conn: sqlite3.Connection) -> set[int]:
    """Return the set of DocNums currently in the cache (any status)."""
    cur = conn.execute("SELECT DocNum FROM doc_text")
    return {int(r[0]) for r in cur.fetchall()}


def terminal_doc_nums(conn: sqlite3.Connection) -> set[int]:
    """Return DocNums whose status is terminal — won't change on retry.

    Used by backfill to decide what to skip. `error` rows are excluded so
    transient failures (rate limits, missing env vars, share hiccups) get
    retried on subsequent runs.
    """
    cur = conn.execute(
        "SELECT DocNum FROM doc_text WHERE Status IN ('ok', 'unreadable', 'unsupported')"
    )
    return {int(r[0]) for r in cur.fetchall()}


def put_text(conn: sqlite3.Connection, row: DocTextRow) -> None:
    """Insert or replace a row. Status must be one of VALID_STATUS."""
    if row.Status not in VALID_STATUS:
        raise ValueError(f"invalid status: {row.Status!r}")
    if row.PatNum is None or row.DocNum is None:
        raise ValueError("DocNum and PatNum are required")
    conn.execute(
        """
        INSERT INTO doc_text
            (DocNum, PatNum, FileName, DocCategory, DateCreated, Text,
             PageCount, Sha256, OcrModel, OcrAt, Status, ErrorMessage, CostUsd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(DocNum) DO UPDATE SET
            PatNum=excluded.PatNum,
            FileName=excluded.FileName,
            DocCategory=excluded.DocCategory,
            DateCreated=excluded.DateCreated,
            Text=excluded.Text,
            PageCount=excluded.PageCount,
            Sha256=excluded.Sha256,
            OcrModel=excluded.OcrModel,
            OcrAt=excluded.OcrAt,
            Status=excluded.Status,
            ErrorMessage=excluded.ErrorMessage,
            CostUsd=excluded.CostUsd
        """,
        (
            int(row.DocNum), int(row.PatNum), row.FileName, row.DocCategory,
            row.DateCreated, row.Text, row.PageCount, row.Sha256,
            row.OcrModel, row.OcrAt, row.Status, row.ErrorMessage, row.CostUsd,
        ),
    )
    conn.commit()


def prune_orphans(conn: sqlite3.Connection, known_doc_nums: Iterable[int]) -> int:
    """Delete cache rows whose DocNum is not in known_doc_nums. Returns deleted count."""
    known_set = {int(x) for x in known_doc_nums}
    if not known_set:
        # Defensive: refuse to nuke the whole cache on an empty input.
        return 0
    # Build a temp table to handle large input sets.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _known (n INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _known")
    conn.executemany("INSERT OR IGNORE INTO _known(n) VALUES (?)", [(n,) for n in known_set])
    cur = conn.execute(
        "DELETE FROM doc_text WHERE DocNum NOT IN (SELECT n FROM _known)"
    )
    conn.commit()
    deleted = cur.rowcount or 0
    return deleted


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _sanitize_fts_query(q: str) -> str:
    """FTS5 special chars: " * + - ^ : ( ). Quote each whitespace-separated
    token to make a safe AND search."""
    tokens = [t for t in q.split() if t.strip()]
    if not tokens:
        return ""
    safe = []
    for t in tokens:
        # Strip embedded quotes; FTS5 quoting is double-quote.
        cleaned = t.replace('"', '')
        if not cleaned:
            continue
        safe.append(f'"{cleaned}"')
    return " ".join(safe)


def search(
    conn: sqlite3.Connection,
    query: str,
    pat_num: Optional[int] = None,
    doc_category: Optional[int] = None,
    k: int = 20,
) -> List[SearchHit]:
    """Substring/keyword search over OCR'd text via FTS5.

    `score` is a non-negative number where lower = better (FTS5 bm25). Caller
    can sort/threshold as desired; we already sort ascending.
    """
    fts_q = _sanitize_fts_query(query or "")
    if not fts_q:
        return []
    base = (
        "SELECT d.DocNum, d.PatNum, d.FileName, d.Status, "
        "snippet(doc_text_fts, 0, '[', ']', '...', 16) AS Snippet, "
        "bm25(doc_text_fts) AS Score "
        "FROM doc_text_fts JOIN doc_text d ON d.DocNum = doc_text_fts.rowid "
        "WHERE doc_text_fts MATCH ? "
    )
    params: list = [fts_q]
    if pat_num is not None:
        base += "AND d.PatNum = ? "
        params.append(int(pat_num))
    if doc_category is not None:
        base += "AND d.DocCategory = ? "
        params.append(int(doc_category))
    base += "ORDER BY Score LIMIT ?"
    params.append(int(k))
    cur = conn.execute(base, params)
    out: list[SearchHit] = []
    for r in cur.fetchall():
        out.append(SearchHit(
            DocNum=int(r["DocNum"]),
            PatNum=int(r["PatNum"]),
            FileName=r["FileName"],
            Snippet=r["Snippet"] or "",
            Score=float(r["Score"]) if r["Score"] is not None else 0.0,
            Status=str(r["Status"]),
        ))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _row_to_dataclass(r: sqlite3.Row) -> DocTextRow:
    return DocTextRow(
        DocNum=int(r["DocNum"]),
        PatNum=int(r["PatNum"]),
        FileName=r["FileName"],
        DocCategory=r["DocCategory"],
        DateCreated=r["DateCreated"],
        Text=r["Text"] or "",
        PageCount=r["PageCount"],
        Sha256=r["Sha256"],
        OcrModel=r["OcrModel"],
        OcrAt=r["OcrAt"],
        Status=r["Status"],
        ErrorMessage=r["ErrorMessage"],
        CostUsd=r["CostUsd"],
    )
