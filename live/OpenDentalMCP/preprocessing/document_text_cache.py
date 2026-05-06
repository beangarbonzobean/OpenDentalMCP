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
from datetime import datetime, timedelta, timezone
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
    # Reviewed columns track the human-in-the-loop quality check from the
    # /ocr-review/ UI. Reviewed=0 -> needs review; 1 -> approved by staff.
    # Approved rows aren't materially different from un-reviewed for the
    # search functionality — the flag is purely so the review UI can hide
    # them from the "needs review" default view.
    Reviewed: int = 0
    ReviewedAt: Optional[str] = None
    ReviewedBy: Optional[str] = None
    # Source/page tracking. od_backfill rows come from the nightly historical-doc
    # OCR pipeline and resolve their on-disk file via the OD patient-folder
    # convention. intake_daytime rows come from the same-day batch-scan intake
    # processor; they aren't in OD yet, so SourcePdfPath points at the original
    # batch PDF on the share and PageIndex selects which page within it.
    Source: str = "od_backfill"
    SourcePdfPath: Optional[str] = None
    PageIndex: Optional[int] = None


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
    CostUsd       REAL,
    Reviewed      INTEGER NOT NULL DEFAULT 0,
    ReviewedAt    TEXT,
    ReviewedBy    TEXT,
    Source        TEXT NOT NULL DEFAULT 'od_backfill',
    SourcePdfPath TEXT,
    PageIndex     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_doc_text_pat ON doc_text(PatNum);
CREATE INDEX IF NOT EXISTS idx_doc_text_sha ON doc_text(Sha256) WHERE Sha256 IS NOT NULL;
-- Indexes on Reviewed and OcrAt are created in _migrate_add_review_columns
-- so they don't fire on legacy DBs that don't yet have those columns.

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
    """Create the cache file and schema if missing. Returns the path used.

    For existing cache files predating the Reviewed columns, the migration
    runs idempotently (column-add only — never DROPs anything).
    """
    p = Path(path) if path is not None else DEFAULT_CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        conn = _connect(p)
        try:
            conn.executescript(_SCHEMA_SQL)
            _migrate_add_review_columns(conn)
            conn.commit()
        finally:
            conn.close()
    return p


def _migrate_add_review_columns(conn: sqlite3.Connection) -> None:
    """Add Reviewed/ReviewedAt/ReviewedBy columns to legacy databases.

    SQLite supports ALTER TABLE ADD COLUMN but not IF NOT EXISTS on it,
    so we check the table's existing columns first.
    """
    cur = conn.execute("PRAGMA table_info(doc_text)")
    have = {r[1] for r in cur.fetchall()}
    if "Reviewed" not in have:
        conn.execute("ALTER TABLE doc_text ADD COLUMN Reviewed INTEGER NOT NULL DEFAULT 0")
    if "ReviewedAt" not in have:
        conn.execute("ALTER TABLE doc_text ADD COLUMN ReviewedAt TEXT")
    if "ReviewedBy" not in have:
        conn.execute("ALTER TABLE doc_text ADD COLUMN ReviewedBy TEXT")
    if "Source" not in have:
        conn.execute(
            "ALTER TABLE doc_text ADD COLUMN Source TEXT NOT NULL DEFAULT 'od_backfill'"
        )
    if "SourcePdfPath" not in have:
        conn.execute("ALTER TABLE doc_text ADD COLUMN SourcePdfPath TEXT")
    if "PageIndex" not in have:
        conn.execute("ALTER TABLE doc_text ADD COLUMN PageIndex INTEGER")
    # Also ensure the new indexes exist on legacy DBs.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_text_ocr_at   ON doc_text(OcrAt)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_text_reviewed ON doc_text(Reviewed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_text_source   ON doc_text(Source)")
    # Sha256 index powers the dedup fast-path in ocr_one_document — same file
    # bytes across patients (form templates, reused consents) reuse the cached
    # text instead of re-OCRing.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_text_sha "
        "ON doc_text(Sha256) WHERE Sha256 IS NOT NULL"
    )


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


def find_ok_by_sha256(
    conn: sqlite3.Connection,
    sha256: str,
    *,
    min_text_chars: int = 1,
) -> Optional[DocTextRow]:
    """Return the oldest (smallest DocNum) Status='ok' row matching this SHA.

    Used by the dedup fast-path in ocr_one_document — when the same file bytes
    appear under multiple DocNums (e.g. a consent template scanned for many
    patients) the new doc reuses the cached text instead of re-OCRing.

    `min_text_chars` lets the caller require a minimum non-trivial text length
    so we don't propagate previously-bad cached results. Default 1 (any non-
    empty Text qualifies; the MIN_OK_CHARS floor in ocr_one_document already
    guarantees ok rows have substantive text, so 1 is a safe lower bound).
    """
    if not sha256:
        return None
    cur = conn.execute(
        "SELECT * FROM doc_text "
        "WHERE Sha256 = ? AND Status = 'ok' AND length(Text) >= ? "
        "ORDER BY DocNum LIMIT 1",
        (sha256, int(min_text_chars)),
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
    # On conflict (re-OCR of an existing DocNum), reset Reviewed=0 because
    # the OCR content may have changed and any prior approval shouldn't
    # automatically carry over to new text.
    conn.execute(
        """
        INSERT INTO doc_text
            (DocNum, PatNum, FileName, DocCategory, DateCreated, Text,
             PageCount, Sha256, OcrModel, OcrAt, Status, ErrorMessage, CostUsd,
             Reviewed, ReviewedAt, ReviewedBy,
             Source, SourcePdfPath, PageIndex)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            CostUsd=excluded.CostUsd,
            Reviewed=0,
            ReviewedAt=NULL,
            ReviewedBy=NULL,
            Source=excluded.Source,
            SourcePdfPath=excluded.SourcePdfPath,
            PageIndex=excluded.PageIndex
        """,
        (
            int(row.DocNum), int(row.PatNum), row.FileName, row.DocCategory,
            row.DateCreated, row.Text, row.PageCount, row.Sha256,
            row.OcrModel, row.OcrAt, row.Status, row.ErrorMessage, row.CostUsd,
            int(row.Reviewed or 0), row.ReviewedAt, row.ReviewedBy,
            row.Source or "od_backfill", row.SourcePdfPath, row.PageIndex,
        ),
    )
    conn.commit()


def put_intake_page_text(
    conn: sqlite3.Connection,
    *,
    source_pdf_sha256: str,
    page_index: int,
    source_pdf_path: str,
    pat_num: Optional[int],
    file_name: Optional[str],
    text: str,
    page_count_in_source: Optional[int],
    ocr_model: Optional[str],
    status: str = "ok",
    error_message: Optional[str] = None,
    cost_usd: Optional[float] = None,
) -> int:
    """Insert (or update) an intake-batch per-page OCR row. Returns the synthetic
    DocNum allocated.

    Identity is (Source='intake_daytime', Sha256=<source pdf sha>, PageIndex).
    Re-running the intake processor on the same PDF (only possible if the
    intake_processed_pdfs row is removed) updates in place rather than
    duplicating rows.

    PatNum=0 is used when the intake matcher has no patient yet — real OD
    PatNums start at 1.
    """
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status!r}")

    # Look up existing row by (Source, Sha256, PageIndex). If it exists,
    # reuse its DocNum so we update rather than allocate a new negative.
    cur = conn.execute(
        "SELECT DocNum FROM doc_text "
        "WHERE Source = 'intake_daytime' AND Sha256 = ? AND PageIndex = ?",
        (source_pdf_sha256, int(page_index)),
    )
    existing = cur.fetchone()
    if existing is not None:
        doc_num = int(existing["DocNum"])
    else:
        # Allocate a fresh synthetic DocNum below the current minimum.
        # Real OD DocNums are positive; intake rows live in (-inf, 0).
        cur = conn.execute("SELECT COALESCE(MIN(DocNum), 0) FROM doc_text")
        cur_min = int(cur.fetchone()[0] or 0)
        doc_num = min(-1, cur_min - 1)

    row = DocTextRow(
        DocNum=doc_num,
        PatNum=int(pat_num) if pat_num else 0,
        FileName=file_name,
        DocCategory=None,
        DateCreated=None,
        Text=text or "",
        PageCount=page_count_in_source,
        Sha256=source_pdf_sha256,
        OcrModel=ocr_model,
        OcrAt=datetime.now(timezone.utc).isoformat(),
        Status=status,
        ErrorMessage=error_message,
        CostUsd=cost_usd,
        Source="intake_daytime",
        SourcePdfPath=source_pdf_path,
        PageIndex=int(page_index),
    )
    put_text(conn, row)
    return doc_num


def mark_reviewed(
    conn: sqlite3.Connection,
    doc_num: int,
    *,
    reviewer: str,
) -> bool:
    """Approve a doc_text row from the OCR review UI. Returns True if a row
    was updated. Does not modify the OCR text — just marks the audit fields.
    """
    cur = conn.execute(
        "UPDATE doc_text SET Reviewed = 1, ReviewedAt = ?, ReviewedBy = ? "
        "WHERE DocNum = ?",
        (datetime.now(timezone.utc).isoformat(), str(reviewer), int(doc_num)),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


def unmark_reviewed(conn: sqlite3.Connection, doc_num: int) -> bool:
    """Reverse a review approval (puts the row back into the queue)."""
    cur = conn.execute(
        "UPDATE doc_text SET Reviewed = 0, ReviewedAt = NULL, ReviewedBy = NULL "
        "WHERE DocNum = ?",
        (int(doc_num),),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


def delete_doc_text(conn: sqlite3.Connection, doc_num: int) -> bool:
    """Drop a single OCR row from the cache. Used by the review UI when a
    user flags an OCR result as bad ("flag for re-OCR"). Next backfill run
    will re-process the doc since it's no longer in terminal_doc_nums.

    Returns True if a row was deleted. The FTS5 trigger also removes the
    row from the search index automatically.
    """
    cur = conn.execute("DELETE FROM doc_text WHERE DocNum = ?", (int(doc_num),))
    conn.commit()
    return (cur.rowcount or 0) > 0


def list_recent_docs(
    conn: sqlite3.Connection,
    *,
    since_iso: Optional[str] = None,
    only_unreviewed: bool = True,
    status_in: Optional[List[str]] = None,
    doc_category: Optional[int] = None,
    source_in: Optional[List[str]] = None,
    limit: int = 200,
    offset: int = 0,
) -> List[DocTextRow]:
    """Recent OCR'd docs for the review UI.

    Default: rows OCR'd in the last 7 days that haven't been reviewed yet.
    Filterable by status, category, source, recency.
    """
    if since_iso is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
        since_iso = since_dt.isoformat()
    base = "SELECT * FROM doc_text WHERE OcrAt >= ?"
    params: list = [since_iso]
    if only_unreviewed:
        base += " AND Reviewed = 0"
    if status_in:
        placeholders = ",".join("?" * len(status_in))
        base += f" AND Status IN ({placeholders})"
        params.extend(status_in)
    if doc_category is not None:
        base += " AND DocCategory = ?"
        params.append(int(doc_category))
    if source_in:
        placeholders = ",".join("?" * len(source_in))
        base += f" AND Source IN ({placeholders})"
        params.extend(source_in)
    base += " ORDER BY OcrAt DESC, DocNum DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return [_row_to_dataclass(r) for r in conn.execute(base, params).fetchall()]


def review_summary(
    conn: sqlite3.Connection,
    *,
    since_iso: Optional[str] = None,
) -> dict:
    """Counts + stats for the review UI dashboard, scoped to a recency window."""
    if since_iso is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
        since_iso = since_dt.isoformat()
    by_status = {}
    for r in conn.execute(
        "SELECT Status, COUNT(*) AS n FROM doc_text WHERE OcrAt >= ? GROUP BY Status",
        (since_iso,),
    ).fetchall():
        by_status[r["Status"]] = int(r["n"])
    total = sum(by_status.values())
    unreviewed = conn.execute(
        "SELECT COUNT(*) FROM doc_text WHERE OcrAt >= ? AND Reviewed = 0",
        (since_iso,),
    ).fetchone()[0]
    cost_total = conn.execute(
        "SELECT COALESCE(SUM(CostUsd), 0) FROM doc_text WHERE OcrAt >= ?",
        (since_iso,),
    ).fetchone()[0]
    last_run = conn.execute(
        "SELECT MAX(OcrAt) FROM doc_text"
    ).fetchone()[0]
    return {
        "since": since_iso,
        "total": total,
        "by_status": by_status,
        "unreviewed": int(unreviewed),
        "cost_usd_total": float(cost_total or 0.0),
        "last_run": last_run,
    }


def prune_orphans(conn: sqlite3.Connection, known_doc_nums: Iterable[int]) -> int:
    """Delete cache rows whose DocNum is not in known_doc_nums. Returns deleted count.

    Scoped to Source='od_backfill' rows — intake_daytime rows have synthetic
    negative DocNums that don't correspond to OD's document table, so the
    nightly backfill's "known DocNums from OD" list never contains them and
    they would otherwise be wiped on every prune.
    """
    known_set = {int(x) for x in known_doc_nums}
    if not known_set:
        # Defensive: refuse to nuke the whole cache on an empty input.
        return 0
    # Build a temp table to handle large input sets.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _known (n INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _known")
    conn.executemany("INSERT OR IGNORE INTO _known(n) VALUES (?)", [(n,) for n in known_set])
    cur = conn.execute(
        "DELETE FROM doc_text "
        "WHERE Source = 'od_backfill' "
        "AND DocNum NOT IN (SELECT n FROM _known)"
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
    keys = r.keys()
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
        Reviewed=int(r["Reviewed"]) if "Reviewed" in keys else 0,
        ReviewedAt=r["ReviewedAt"] if "ReviewedAt" in keys else None,
        ReviewedBy=r["ReviewedBy"] if "ReviewedBy" in keys else None,
        Source=r["Source"] if "Source" in keys and r["Source"] else "od_backfill",
        SourcePdfPath=r["SourcePdfPath"] if "SourcePdfPath" in keys else None,
        PageIndex=r["PageIndex"] if "PageIndex" in keys else None,
    )
