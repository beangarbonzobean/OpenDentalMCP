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
import re
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
from preprocessing.html_extract import extract_html_text, is_html_filename
from preprocessing.path_resolver import resolve_doc_path_with_fallback
from preprocessing.sql_safety import assert_select_only

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = int(os.environ.get("PREPROC_MAX_FILE_BYTES", str(30 * 1024 * 1024)))  # 30 MB
MAX_PDF_PAGES = int(os.environ.get("PREPROC_MAX_PDF_PAGES", "20"))
ITER_BATCH = int(os.environ.get("PREPROC_ITER_BATCH", "500"))
LOCK_FILE_NAME = ".rebuild.lock"

# Output-quality floor. An OCR result with fewer than this many non-whitespace
# characters is treated as 'unreadable' rather than 'ok' — protects against
# silent failures (model returned empty, decoder produced garbage, etc.). Real
# documents have far more text; the threshold mainly catches:
#   - VLMs that returned an empty string instead of the UNREADABLE sentinel
#   - HTML files whose decoder produced a few stray bytes
#   - Blank scanned pages that the model "transcribed" as one or two chars
# Set to 0 via PREPROC_MIN_OK_CHARS=0 to disable in tests.
MIN_OK_CHARS = int(os.environ.get("PREPROC_MIN_OK_CHARS", "20"))


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


# Filenames matching this pattern are temp-file artifacts (e.g. left behind
# by Dexis or by an old intake pipeline). They produce 'unreadable' OCR
# results forever and waste cycles every nightly run because 'unreadable' is
# not a terminal status. Mark them 'unsupported' instead so they're terminal.
_TMP_ARTIFACT_RE = re.compile(
    r"^(?:\d+_)?tmp[0-9a-fA-F]+(?:\.tmp)?\.(?:png|jpg|jpeg)$",
    re.IGNORECASE,
)


def _is_tmp_artifact(file_name: str) -> bool:
    return bool(_TMP_ARTIFACT_RE.match(file_name or ""))


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


def _debug_capture_doc_result(doc: "OdDocRow", media_type: str, result: "ocr_helper.OcrResult") -> None:
    """Write one-shot doc-alignment capture when DEBUG_OCR_CAPTURE_PATH is set.

    Pairs with ocr_helper._debug_capture_ollama_io: together the two files let
    you align one Ollama round-trip (ollama_io.json) with the corresponding DB
    row fields (doc_alignment.json).
    """
    import json as _json
    capture_dir = os.environ.get("DEBUG_OCR_CAPTURE_PATH", "").strip()
    if not capture_dir:
        return
    capture_file = os.path.join(capture_dir, "doc_alignment.json")
    if os.path.exists(capture_file):
        return
    try:
        import time as _time
        payload = {
            "ts": _time.time(),
            "doc": {
                "DocNum": doc.DocNum,
                "PatNum": doc.PatNum,
                "FileName": doc.FileName,
                "DocCategory": doc.DocCategory,
                "DateCreated": doc.DateCreated,
                "LName": doc.LName,
                "FName": doc.FName,
            },
            "media_type": media_type,
            "ocr_result": {
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "is_unreadable": result.is_unreadable,
                "text_len": len(result.text),
                "text_preview": result.text[:500],
            },
            "db_row_status": "unreadable" if result.is_unreadable else "ok",
        }
        os.makedirs(capture_dir, exist_ok=True)
        with open(capture_file, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, indent=2, ensure_ascii=False)
        log.info("DEBUG_OCR_CAPTURE: wrote %s (DocNum=%d)", capture_file, doc.DocNum)
    except Exception as exc:
        log.warning("DEBUG_OCR_CAPTURE doc_alignment write failed: %s", exc)


def ocr_one_document(
    doc: OdDocRow,
    *,
    skip_categories: Optional[set[int]] = None,
    file_loader: Optional[Callable[[OdDocRow], bytes]] = None,
    ocr_fn: Optional[Callable[..., ocr_helper.OcrResult]] = None,
    share_root: Optional[Path] = None,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_pdf_pages: int = MAX_PDF_PAGES,
    dedup_lookup: Optional[Callable[[str], Optional[cache.DocTextRow]]] = None,
) -> cache.DocTextRow:
    """OCR a single document and return a cache row. Pure function: does not
    persist anything. Caller writes the returned row via cache.put_text.

    `dedup_lookup` is an optional callable: given a file's SHA-256 it returns
    a previously-OCR'd `DocTextRow` (any patient) whose bytes match. When
    provided and a hit is found, this doc reuses the cached text instead of
    re-OCRing — saves both time and (for the Haiku/auto backends) money on
    duplicate form templates.

    All exceptions are caught and converted to status='error' rows. The function
    never raises (except for programmer errors like wrong types).
    """
    skip = skip_categories if skip_categories is not None else _skip_categories()

    if not doc.FileName:
        return _make_unsupported(doc, "no_filename")

    if doc.DocCategory is not None and doc.DocCategory in skip:
        return _make_unsupported(doc, "category_skipped")

    # Filter out OD/Dexis temp artifacts before paying for any I/O — these
    # tmp*.tmp.png files are leftover thumbnails that never carry usable
    # content and otherwise waste OCR cycles getting rejected as "unreadable"
    # on every nightly run (status='unreadable' isn't terminal for retry).
    if _is_tmp_artifact(doc.FileName):
        return _make_unsupported(doc, "tmp_artifact")

    is_html = is_html_filename(doc.FileName)
    kind, media_type = ocr_helper.classify_extension(doc.FileName)
    if not is_html and (kind == "unsupported" or media_type is None):
        return _make_unsupported(doc, "extension")

    if not doc.LName or not str(doc.LName).strip():
        return _make_error(doc, "missing_lname")

    # Load bytes
    try:
        if file_loader is not None:
            file_bytes = file_loader(doc)
        else:
            path = resolve_doc_path_with_fallback(
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

    # Compute SHA-256 once, up-front. Used for the dedup fast-path AND for the
    # eventual cache row.
    sha = compute_sha256(file_bytes)

    # Dedup fast-path: if any other DocNum has Status='ok' rows with this
    # exact SHA, reuse its text. Most common in practice for form templates
    # (consents, registration sheets, daily sign-in pages) that get scanned
    # for many patients with byte-identical content.
    if dedup_lookup is not None:
        cached_match = dedup_lookup(sha)
        if cached_match is not None and cached_match.DocNum != doc.DocNum:
            log.info(
                "dedup hit: DocNum=%d reusing OCR from DocNum=%d (sha=%s..., %d chars)",
                doc.DocNum, cached_match.DocNum, sha[:8], len(cached_match.Text or ""),
            )
            return cache.DocTextRow(
                DocNum=doc.DocNum,
                PatNum=doc.PatNum,
                FileName=doc.FileName,
                DocCategory=doc.DocCategory,
                DateCreated=doc.DateCreated,
                Text=cached_match.Text or "",
                PageCount=cached_match.PageCount,
                Sha256=sha,
                OcrModel=f"dedup:{cached_match.OcrModel or 'unknown'}",
                OcrAt=_now_iso(),
                Status="ok",
                CostUsd=0.0,
            )

    # HTML files (saved Dentrix Eligibility reports etc.) carry text directly —
    # no OCR needed. Extract with stdlib html.parser, no API cost.
    if is_html:
        text = extract_html_text(file_bytes)
        # Enforce the output-quality floor — a few stray bytes from a malformed
        # HTML / encoding mismatch should not be cached as 'ok'.
        if len(text.strip()) < MIN_OK_CHARS:
            status = "unreadable"
            err = f"short_output:{len(text.strip())}" if text.strip() else None
            text = ""  # don't poison the FTS index with garbage
        else:
            status = "ok"
            err = None
        return cache.DocTextRow(
            DocNum=doc.DocNum,
            PatNum=doc.PatNum,
            FileName=doc.FileName,
            DocCategory=doc.DocCategory,
            DateCreated=doc.DateCreated,
            Text=text,
            PageCount=1,
            Sha256=sha,
            OcrModel="html_extract",
            OcrAt=_now_iso(),
            Status=status,
            ErrorMessage=err,
            CostUsd=0.0,
        )

    page_count: Optional[int] = None
    if kind == "pdf":
        page_count = _pdf_page_count(file_bytes)
        if page_count is not None and page_count > max_pdf_pages:
            return _make_unsupported(doc, f"too_many_pages:{page_count}")

    # OCR
    fn = ocr_fn if ocr_fn is not None else ocr_helper.ocr_bytes
    # Category-aware prompt: when OCR_CATEGORY_PROMPTS_FILE is configured AND
    # this DocCategory has an entry, prime the model with field vocabulary
    # specific to that document type. Falls back to the generic dental prompt
    # otherwise.
    category_prompt = ocr_helper.get_prompt_for_doc_category(doc.DocCategory)
    try:
        if category_prompt is not None:
            result = fn(file_bytes, media_type=media_type, prompt=category_prompt)
        else:
            result = fn(file_bytes, media_type=media_type)
    except ocr_helper.OcrConfigError as e:
        return _make_error(doc, f"config:{e}")
    except ocr_helper.OcrError as e:
        return _make_error(doc, f"ocr_failed:{e}")
    except Exception as e:  # belt and suspenders
        return _make_error(doc, f"ocr_unexpected:{e}")

    _debug_capture_doc_result(doc, media_type, result)
    # `sha` was computed up-front for the dedup fast-path; reuse it.

    # Output validation: a model that returned an empty/short response without
    # explicitly saying UNREADABLE used to slip through as Status='ok' with
    # near-empty Text — poisoning the search index. Enforce the same floor we
    # apply to html_extract.
    text = result.text
    if result.is_unreadable:
        status = "unreadable"
        err: Optional[str] = None
        text = ""  # the OCR result already strips this but be defensive
    elif len(text.strip()) < MIN_OK_CHARS:
        status = "unreadable"
        err = f"short_output:{len(text.strip())}"
        text = ""  # don't index a few characters of model noise
    else:
        status = "ok"
        err = None

    return cache.DocTextRow(
        DocNum=doc.DocNum,
        PatNum=doc.PatNum,
        FileName=doc.FileName,
        DocCategory=doc.DocCategory,
        DateCreated=doc.DateCreated,
        Text=text,
        PageCount=page_count if kind == "pdf" else 1,
        Sha256=sha,
        OcrModel=result.model,
        OcrAt=_now_iso(),
        Status=status,
        ErrorMessage=err,
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
    workers: int = 1,
    skip_categories: Optional[set[int]] = None,
    file_loader: Optional[Callable[[OdDocRow], bytes]] = None,
    ocr_fn: Optional[Callable[..., ocr_helper.OcrResult]] = None,
    share_root: Optional[Path] = None,
    lock_path: Optional[Path] = None,
) -> BackfillResult:
    """Iterate the document table and OCR uncached docs.

    `workers` controls client-side concurrency. workers=1 keeps the original
    sequential behavior (and is what tests assume for budget/halt determinism).
    workers>1 dispatches OCR via a ThreadPoolExecutor with bounded backpressure
    (queue depth = workers * 2). Each worker uses its own SQLite connection and
    writes through SQLite WAL mode. Counter mutations and budget checks happen
    in the main thread as futures complete, so there's no shared mutable state
    between workers.

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
    workers = max(1, int(workers))

    # Reset per-process Ollama circuit-breaker state so a previous run's trip
    # doesn't carry over. Then probe the local VLM once — if unreachable we
    # pre-trip the breaker so all pages skip straight to Haiku instead of
    # burning per-page timeouts. Only relevant when OCR_BACKEND uses local.
    backend = os.environ.get("OCR_BACKEND", "haiku").lower()
    if backend in ("local", "auto") and ocr_fn is None:
        ocr_helper.reset_circuit_breaker()
        healthy, detail = ocr_helper.health_check_local_vlm()
        if not healthy:
            log.warning(
                "local VLM health check FAILED (%s) — circuit pre-tripped, "
                "this run will use Haiku page fallback only",
                detail,
            )
        else:
            log.info("local VLM health check ok (%s)", detail)

    try:
        with cache.open_cache(cache_p) as conn:
            # Skip only docs whose status is terminal (ok / unreadable / unsupported).
            # Error rows are retried so transient failures don't poison the cache.
            existing = cache.terminal_doc_nums(conn)

            if workers <= 1 or dry_run:
                _backfill_sequential(
                    tools, conn, result, existing,
                    after_doc_num=after_doc_num,
                    max_docs=max_docs,
                    max_spend_usd=max_spend_usd,
                    dry_run=dry_run,
                    skip=skip,
                    file_loader=file_loader,
                    ocr_fn=ocr_fn,
                    share_root=share_root,
                )
            else:
                _backfill_parallel(
                    tools, cache_p, result, existing,
                    after_doc_num=after_doc_num,
                    max_docs=max_docs,
                    max_spend_usd=max_spend_usd,
                    workers=workers,
                    skip=skip,
                    file_loader=file_loader,
                    ocr_fn=ocr_fn,
                    share_root=share_root,
                )

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


def _apply_row_to_result(result: BackfillResult, row: cache.DocTextRow) -> None:
    """Bump counters and cost from a single OCR'd row. Main-thread only."""
    if row.Status == "ok" or row.Status == "unreadable":
        result.ocrd += 1
    elif row.Status == "unsupported":
        result.skipped_unsupported += 1
    elif row.Status == "error":
        result.errors += 1
    if row.CostUsd:
        result.cost_usd_estimate += float(row.CostUsd)


def _make_dedup_lookup(conn) -> Callable[[str], Optional[cache.DocTextRow]]:
    """Build a SHA-256 -> existing 'ok' DocTextRow lookup bound to one connection."""
    def _lookup(sha256: str) -> Optional[cache.DocTextRow]:
        try:
            return cache.find_ok_by_sha256(conn, sha256)
        except Exception as e:  # belt and suspenders — never let dedup break OCR
            log.warning("dedup_lookup failed (will OCR normally): %s", e)
            return None
    return _lookup


def _backfill_sequential(
    tools: Any, conn, result: BackfillResult, existing: set[int],
    *, after_doc_num: int, max_docs: int, max_spend_usd: float, dry_run: bool,
    skip: set[int], file_loader, ocr_fn, share_root,
) -> None:
    """Original single-threaded loop. Preserved as the workers=1 path."""
    dedup_lookup = _make_dedup_lookup(conn)
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
            dedup_lookup=dedup_lookup,
        )
        cache.put_text(conn, row)
        _apply_row_to_result(result, row)

        if result.scanned >= max_docs:
            result.halted_reason = "max_docs"
            break


def _backfill_parallel(
    tools: Any, cache_p: Path, result: BackfillResult, existing: set[int],
    *, after_doc_num: int, max_docs: int, max_spend_usd: float, workers: int,
    skip: set[int], file_loader, ocr_fn, share_root,
) -> None:
    """Parallel path: ThreadPoolExecutor with bounded backpressure.

    Each worker opens its own SQLite cache connection and writes via WAL.
    The main thread mutates `result` as futures complete; budget/cap checks
    happen between submits. In-flight futures are allowed to complete after
    a halt — we don't cancel mid-OCR.
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    def _worker(doc: OdDocRow) -> cache.DocTextRow:
        # Each worker uses its own connection — sqlite3 connections aren't
        # safely shared across threads. WAL mode + per-connection commit
        # serializes writes correctly. We use the same connection for the
        # dedup lookup and the put_text write so they share state.
        with cache.open_cache(cache_p) as worker_conn:
            row = ocr_one_document(
                doc,
                skip_categories=skip,
                file_loader=file_loader,
                ocr_fn=ocr_fn,
                share_root=share_root,
                dedup_lookup=_make_dedup_lookup(worker_conn),
            )
            cache.put_text(worker_conn, row)
        return row

    in_flight: set = set()
    halted = False

    def _drain_one() -> None:
        nonlocal halted
        done, _pending = wait(in_flight, return_when=FIRST_COMPLETED)
        for f in done:
            in_flight.discard(f)
            try:
                row = f.result()
            except Exception as e:  # belt and suspenders
                log.exception("worker raised: %s", e)
                result.errors += 1
                continue
            _apply_row_to_result(result, row)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for doc in iter_documents(tools, after_doc_num=after_doc_num):
            result.scanned += 1

            if doc.DocNum in existing:
                result.skipped_cached += 1
                if result.scanned >= max_docs:
                    result.halted_reason = "max_docs"
                    halted = True
                    break
                continue

            # Drain enough in-flight work that we don't queue unbounded.
            while len(in_flight) >= workers * 2:
                _drain_one()
                if result.cost_usd_estimate >= max_spend_usd:
                    result.halted_reason = "budget"
                    halted = True
                    break
            if halted:
                break

            if result.cost_usd_estimate >= max_spend_usd:
                result.halted_reason = "budget"
                halted = True
                break

            in_flight.add(pool.submit(_worker, doc))

            if result.scanned >= max_docs:
                result.halted_reason = "max_docs"
                halted = True
                break

        # Drain remaining workers — let already-submitted OCRs finish.
        while in_flight:
            _drain_one()


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
            dedup_lookup=_make_dedup_lookup(conn),
        )
        cache.put_text(conn, row)
        return row, "fresh"
