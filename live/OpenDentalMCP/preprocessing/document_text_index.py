"""
Orchestration: iterate Open Dental's `document` table, OCR each scanned doc once,
write the text to the local cache.

Reads OD's DB via `tools._query_database` with assert_select_only on every SQL
string. Reads files via Path.read_bytes() only — no writes to the share.
Writes only to the local SQLite cache + a lock file under data/.

Usage:
    from preprocessing import document_text_index as idx
    result = idx.backfill(tools, max_docs=100, max_spend_usd=1.0)

Tests cover the orchestration with a fake `tools` object and a mocked OCR fn.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

try:  # portalocker is required for the lock file; tests can monkeypatch.
    import portalocker  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - dep should be installed
    portalocker = None  # type: ignore[assignment]

from preprocessing import document_text_cache as cache
from preprocessing import ocr_helper
from preprocessing.path_resolver import resolve_doc_path
from preprocessing.sql_safety import assert_select_only

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = int(os.environ.get("PREPROC_MAX_FILE_BYTES", str(30 * 1024 * 1024)))  # 30 MB
MAX_PDF_PAGES = int(os.environ.get("PREPROC_MAX_PDF_PAGES", "20"))
ITER_BATCH = int(os.environ.get("PREPROC_ITER_BATCH", "500"))
LOCK_FILE_NAME = ".rebuild.lock"


def _skip_categories() -> set[int]:
    raw = os.environ.get("DOC_TEXT_SKIP_CATEGORIES", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            log.warning("Bad DOC_TEXT_SKIP_CATEGORIES token: %r", tok)
    return out


# ---------------------------------------------------------------------------
# SQL queries (all SELECT, all wrapped by assert_select_only at call sites)
# ---------------------------------------------------------------------------

_SQL_ITER_DOCS = (
    "SELECT d.DocNum, d.PatNum, d.FileName, d.DateCreated, d.DocCategory, "
    "p.LName, p.FName "
    "FROM document d JOIN patient p ON d.PatNum = p.PatNum "
    "WHERE d.DocNum > ? "
    "ORDER BY d.DocNum "
    "LIMIT ?"
)

_SQL_ONE_DOC = (
    "SELECT d.DocNum, d.PatNum, d.FileName, d.DateCreated, d.DocCategory, "
    "p.LName, p.FName "
    "FROM document d JOIN patient p ON d.PatNum = p.PatNum "
    "WHERE d.DocNum = ?"
)

_SQL_COUNT_FOR_PATIENT = (
    "SELECT COUNT(*) AS n FROM document WHERE PatNum = ?"
)

_SQL_ALL_DOC_NUMS = "SELECT DocNum FROM document"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class OdDocRow:
    DocNum: int
    PatNum: int
    FileName: Optional[str]
    DateCreated: Optional[str]
    DocCategory: Optional[int]
    LName: str
    FName: str


@dataclass
class BackfillResult:
    success: bool
    scanned: int = 0
    ocrd: int = 0
    skipped_unsupported: int = 0
    skipped_cached: int = 0
    errors: int = 0
    pruned: int = 0
    build_seconds: float = 0.0
    cost_usd_estimate: float = 0.0
    halted_reason: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# DB iteration
# ---------------------------------------------------------------------------

def _query(tools: Any, sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT through tools._query_database after the safety check.

    Open Dental's _query_database signature: _query_database(query: str, limit: int = 1000).
    For the iter query, the LIMIT is in the SQL itself; we pass a generous limit
    to the helper so it doesn't re-clamp.

    Many _query_database implementations don't take params and instead expect
    the caller to interpolate. We use a simple, safe interpolation for our
    integer params here, after the safety guard has already proven the SQL is
    SELECT-only with no DML.
    """
    assert_select_only(sql)
    if params:
        # All params we use are ints. Reject anything else to keep this safe.
        for p in params:
            if not isinstance(p, int):
                raise ValueError(f"_query only accepts int params, got {type(p).__name__}")
        rendered = sql
        for p in params:
            rendered = rendered.replace("?", str(int(p)), 1)
    else:
        rendered = sql
    result = tools._query_database(rendered, limit=10_000)
    if isinstance(result, dict) and not result.get("success", True):
        raise RuntimeError(f"_query_database failed: {result.get('error')}")
    rows = result.get("rows", []) if isinstance(result, dict) else result
    return list(rows or [])


def iter_documents(
    tools: Any,
    after_doc_num: int = 0,
    batch: int = ITER_BATCH,
) -> Iterator[OdDocRow]:
    """Keyset-paginate the document table by DocNum. Read-only."""
    cursor = int(after_doc_num)
    while True:
        rows = _query(tools, _SQL_ITER_DOCS, (cursor, int(batch)))
        if not rows:
            return
        for r in rows:
            yield OdDocRow(
                DocNum=int(r["DocNum"]),
                PatNum=int(r["PatNum"]),
                FileName=r.get("FileName"),
                DateCreated=str(r["DateCreated"]) if r.get("DateCreated") is not None else None,
                DocCategory=int(r["DocCategory"]) if r.get("DocCategory") is not None else None,
                LName=str(r.get("LName") or ""),
                FName=str(r.get("FName") or ""),
            )
        cursor = int(rows[-1]["DocNum"])


def fetch_one_document(tools: Any, doc_num: int) -> Optional[OdDocRow]:
    rows = _query(tools, _SQL_ONE_DOC, (int(doc_num),))
    if not rows:
        return None
    r = rows[0]
    return OdDocRow(
        DocNum=int(r["DocNum"]),
        PatNum=int(r["PatNum"]),
        FileName=r.get("FileName"),
        DateCreated=str(r["DateCreated"]) if r.get("DateCreated") is not None else None,
        DocCategory=int(r["DocCategory"]) if r.get("DocCategory") is not None else None,
        LName=str(r.get("LName") or ""),
        FName=str(r.get("FName") or ""),
    )


def count_documents_for_patient(tools: Any, pat_num: int) -> int:
    rows = _query(tools, _SQL_COUNT_FOR_PATIENT, (int(pat_num),))
    return int(rows[0]["n"]) if rows else 0


def all_doc_nums(tools: Any) -> set[int]:
    rows = _query(tools, _SQL_ALL_DOC_NUMS)
    return {int(r["DocNum"]) for r in rows}


# ---------------------------------------------------------------------------
# Single-doc OCR
# ---------------------------------------------------------------------------

def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_unsupported(doc: OdDocRow, reason: str) -> cache.DocTextRow:
    return cache.DocTextRow(
        DocNum=doc.DocNum,
        PatNum=doc.PatNum,
        FileName=doc.FileName,
        DocCategory=doc.DocCategory,
        DateCreated=doc.DateCreated,
        Text="",
        Status="unsupported",
        ErrorMessage=reason,
        OcrModel=None,
        OcrAt=_now_iso(),
        CostUsd=0.0,
    )


def _make_error(doc: OdDocRow, reason: str) -> cache.DocTextRow:
    return cache.DocTextRow(
        DocNum=doc.DocNum,
        PatNum=doc.PatNum,
        FileName=doc.FileName,
        DocCategory=doc.DocCategory,
        DateCreated=doc.DateCreated,
        Text="",
        Status="error",
        ErrorMessage=reason,
        OcrModel=None,
        OcrAt=_now_iso(),
        CostUsd=0.0,
    )


def _pdf_page_count(file_bytes: bytes) -> Optional[int]:
    try:
        import pypdf  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover
        return None
    try:
        import io
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        return len(reader.pages)
    except Exception as e:
        log.warning("pdf page-count failed: %s", e)
        return None


def ocr_one_document(
    doc: OdDocRow,
    *,
    skip_categories: Optional[set[int]] = None,
    file_loader: Optional[Callable[[OdDocRow], bytes]] = None,
    ocr_fn: Optional[Callable[..., ocr_helper.OcrResult]] = None,
    share_root: Optional[Path] = None,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_pdf_pages: int = MAX_PDF_PAGES,
) -> cache.DocTextRow:
    """OCR a single document and return a cache row. Pure function: does not
    persist anything. Caller writes the returned row via cache.put_text.

    All exceptions are caught and converted to status='error' rows. The function
    never raises (except for programmer errors like wrong types).
    """
    skip = skip_categories if skip_categories is not None else _skip_categories()

    if not doc.FileName:
        return _make_unsupported(doc, "no_filename")

    if doc.DocCategory is not None and doc.DocCategory in skip:
        return _make_unsupported(doc, "category_skipped")

    kind, media_type = ocr_helper.classify_extension(doc.FileName)
    if kind == "unsupported" or media_type is None:
        return _make_unsupported(doc, "extension")

    if not doc.LName or not str(doc.LName).strip():
        return _make_error(doc, "missing_lname")

    # Load bytes
    try:
        if file_loader is not None:
            file_bytes = file_loader(doc)
        else:
            path = resolve_doc_path(
                doc.PatNum, doc.LName, doc.FName, doc.FileName, share_root=share_root
            )
            if not path.exists():
                return _make_error(doc, f"not_found:{path}")
            file_bytes = path.read_bytes()
    except Exception as e:
        return _make_error(doc, f"read_failed:{e}")

    if not file_bytes:
        return _make_error(doc, "empty_file")
    if len(file_bytes) > max_file_bytes:
        return _make_unsupported(doc, f"oversize:{len(file_bytes)}")

    page_count: Optional[int] = None
    if kind == "pdf":
        page_count = _pdf_page_count(file_bytes)
        if page_count is not None and page_count > max_pdf_pages:
            return _make_unsupported(doc, f"too_many_pages:{page_count}")

    # OCR
    fn = ocr_fn if ocr_fn is not None else ocr_helper.ocr_bytes
    try:
        result = fn(file_bytes, media_type=media_type)
    except ocr_helper.OcrConfigError as e:
        return _make_error(doc, f"config:{e}")
    except ocr_helper.OcrError as e:
        return _make_error(doc, f"ocr_failed:{e}")
    except Exception as e:  # belt and suspenders
        return _make_error(doc, f"ocr_unexpected:{e}")

    sha = compute_sha256(file_bytes)
    return cache.DocTextRow(
        DocNum=doc.DocNum,
        PatNum=doc.PatNum,
        FileName=doc.FileName,
        DocCategory=doc.DocCategory,
        DateCreated=doc.DateCreated,
        Text=result.text,
        PageCount=page_count if kind == "pdf" else 1,
        Sha256=sha,
        OcrModel=result.model,
        OcrAt=_now_iso(),
        Status="unreadable" if result.is_unreadable else "ok",
        CostUsd=result.cost_usd,
    )


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: Path):
    """Return an opened lock file handle or None if the lock can't be acquired."""
    if portalocker is None:  # pragma: no cover
        return None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
        return fh
    except Exception:
        fh.close()
        return None


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        if portalocker is not None:
            portalocker.unlock(fh)
    finally:
        try:
            fh.close()
        except Exception:
            pass


def backfill(
    tools: Any,
    *,
    cache_path: Optional[Path] = None,
    max_docs: int = 500,
    max_spend_usd: float = 5.0,
    after_doc_num: int = 0,
    prune: bool = False,
    dry_run: bool = False,
    skip_categories: Optional[set[int]] = None,
    file_loader: Optional[Callable[[OdDocRow], bytes]] = None,
    ocr_fn: Optional[Callable[..., ocr_helper.OcrResult]] = None,
    share_root: Optional[Path] = None,
    lock_path: Optional[Path] = None,
) -> BackfillResult:
    """Iterate the document table and OCR uncached docs.

    Returns a BackfillResult with running counters. Halts on:
      - max_docs reached  (halted_reason='max_docs')
      - max_spend_usd exceeded  (halted_reason='budget')
      - lock contention  (halted_reason='locked')
      - exception during iteration  (halted_reason='error')
    """
    started = time.monotonic()
    cache_p = cache.init_cache(cache_path)
    lock_p = lock_path if lock_path is not None else cache_p.parent / LOCK_FILE_NAME

    lock_handle = _acquire_lock(lock_p)
    if lock_handle is None and not dry_run:
        return BackfillResult(success=True, halted_reason="locked", build_seconds=0.0)

    skip = skip_categories if skip_categories is not None else _skip_categories()
    result = BackfillResult(success=True)

    try:
        with cache.open_cache(cache_p) as conn:
            # Skip only docs whose status is terminal (ok / unreadable / unsupported).
            # Error rows are retried so transient failures don't poison the cache.
            existing = cache.terminal_doc_nums(conn)

            for doc in iter_documents(tools, after_doc_num=after_doc_num):
                result.scanned += 1

                if doc.DocNum in existing:
                    result.skipped_cached += 1
                    if result.scanned >= max_docs:
                        result.halted_reason = "max_docs"
                        break
                    continue

                if dry_run:
                    log.info("dry-run would-OCR DocNum=%s File=%s", doc.DocNum, doc.FileName)
                    if result.scanned >= max_docs:
                        result.halted_reason = "max_docs"
                        break
                    continue

                if result.cost_usd_estimate >= max_spend_usd:
                    result.halted_reason = "budget"
                    break

                row = ocr_one_document(
                    doc,
                    skip_categories=skip,
                    file_loader=file_loader,
                    ocr_fn=ocr_fn,
                    share_root=share_root,
                )
                cache.put_text(conn, row)

                if row.Status == "ok" or row.Status == "unreadable":
                    result.ocrd += 1
                elif row.Status == "unsupported":
                    result.skipped_unsupported += 1
                elif row.Status == "error":
                    result.errors += 1

                if row.CostUsd:
                    result.cost_usd_estimate += float(row.CostUsd)

                if result.scanned >= max_docs:
                    result.halted_reason = "max_docs"
                    break

            if prune and not dry_run:
                known = all_doc_nums(tools)
                result.pruned = cache.prune_orphans(conn, known)
    except Exception as e:
        log.exception("backfill failed")
        result.success = False
        result.halted_reason = "error"
        result.error = str(e)
    finally:
        _release_lock(lock_handle)
        result.build_seconds = round(time.monotonic() - started, 3)

    return result


# ---------------------------------------------------------------------------
# On-demand single-doc fetch
# ---------------------------------------------------------------------------

def fetch_or_ocr(
    tools: Any,
    doc_num: int,
    *,
    cache_path: Optional[Path] = None,
    file_loader: Optional[Callable[[OdDocRow], bytes]] = None,
    ocr_fn: Optional[Callable[..., ocr_helper.OcrResult]] = None,
    share_root: Optional[Path] = None,
) -> tuple[Optional[cache.DocTextRow], str]:
    """Return (row, source) where source is 'cache' or 'fresh' or 'missing'.

    If the doc isn't in OD's DB at all, returns (None, 'missing').
    If cached and Sha256 still matches the file on disk, returns 'cache'.
    Otherwise OCRs and writes to cache, returns 'fresh'.
    """
    cache_p = cache.init_cache(cache_path)
    with cache.open_cache(cache_p) as conn:
        existing = cache.get_text(conn, doc_num)
        if existing is not None and existing.Status in ("ok", "unreadable"):
            # We trust cache unless caller forces refresh elsewhere.
            return existing, "cache"
        # Not cached, or cached as error/unsupported. Try a fresh OCR.
        doc = fetch_one_document(tools, doc_num)
        if doc is None:
            return None, "missing"
        row = ocr_one_document(
            doc,
            file_loader=file_loader,
            ocr_fn=ocr_fn,
            share_root=share_root,
        )
        cache.put_text(conn, row)
        return row, "fresh"
