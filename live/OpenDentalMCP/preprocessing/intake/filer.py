"""
File a vetted intake document into Open Dental.

This is the first module in the preprocessing layer that intentionally
causes a side effect outside our local cache. The strategy is to route ALL
writes through Open Dental's REST API (`POST /documents`), which handles
BOTH the file write to the image share AND the INSERT into OD's `document`
table internally. From our process's perspective:

  - We never call open() in write mode against the OD share.
  - We never execute INSERT/UPDATE/DELETE SQL against OD's database.
  - We only perform an HTTP POST to OD's REST API.

The existing AST safety-contract test continues to pass — direct share
writes and DB writes are still forbidden in every preprocessing/* module
including this one.

Inputs:
  - source_pdf_bytes: the original batch PDF (entire file)
  - page_indices: which pages of the batch comprise this document
  - pat_num, def_num: target patient and DocCategory
  - od_uploader: callable wrapping tools._upload_document (test seam)

Output: FileResult with success flag, DocNum on success, and a structured
error otherwise. Never raises.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


@dataclass
class FileResult:
    success: bool
    doc_num: Optional[int] = None
    file_name: Optional[str] = None
    file_path: Optional[str] = None  # If OD returns one
    error: Optional[str] = None
    simulated: bool = False  # True if disconnect mode — nothing was actually filed


def file_document(
    *,
    source_pdf_bytes: bytes,
    page_indices: list[int],
    pat_num: int,
    def_num: int,
    description: str = "",
    file_name_hint: Optional[str] = None,
    od_uploader: Callable[[dict], Any],
    disconnect: bool = False,
) -> FileResult:
    """File one document into OD.

    Steps:
      1. Render the requested page_indices into a single combined PDF.
      2. Compute a stable file name (uses the hint or generates one).
      3. Base64-encode the bytes.
      4. Call od_uploader with the OD POST /documents payload.
      5. Parse the response and return a FileResult.

    `disconnect=True` short-circuits step 4: the function still validates,
    extracts pages, and computes the filename, but never invokes
    `od_uploader`. Returns a FileResult with `simulated=True`. Used for
    shadow-mode operation where we want to record what we WOULD have filed
    without actually mutating OD.

    The function never raises. Errors are returned in FileResult.error.
    """
    if not source_pdf_bytes:
        return FileResult(success=False, error="source_pdf_empty")
    if not page_indices:
        return FileResult(success=False, error="no_page_indices")
    try:
        pat_num_i = int(pat_num)
        def_num_i = int(def_num)
    except (TypeError, ValueError):
        return FileResult(success=False, error="invalid_pat_or_def_num")

    # 1. Render pages into a new PDF.
    try:
        out_pdf = _extract_pages_to_pdf(source_pdf_bytes, page_indices)
    except Exception as e:
        log.warning("filer: page extraction failed: %s", e)
        return FileResult(success=False, error=f"page_extract_failed:{e}")

    if not out_pdf:
        return FileResult(success=False, error="empty_output_pdf")

    # 2. File name.
    fname = _make_file_name(file_name_hint, pat_num_i)

    # 3. Encode.
    b64 = base64.b64encode(out_pdf).decode("ascii")

    # 4. Upload via OD REST API. Payload follows tools._upload_document's expected shape.
    payload = {
        "patient_id": pat_num_i,
        "file_name": fname,
        "file_data": b64,
        "description": description or "",
        "category": def_num_i,
    }

    # Disconnect mode: skip the actual upload but report success so the row
    # transitions to simulated_filed and downstream comparison logic can see
    # what we'd have filed.
    if disconnect:
        log.info(
            "filer: SIMULATED upload pat=%d def=%d pages=%s file=%s bytes=%d (disconnect mode)",
            pat_num_i, def_num_i, page_indices, fname, len(out_pdf),
        )
        return FileResult(
            success=True, doc_num=None, file_name=fname,
            file_path=None, simulated=True,
        )

    log.info(
        "filer: uploading to OD pat=%d def=%d pages=%s file=%s bytes=%d",
        pat_num_i, def_num_i, page_indices, fname, len(out_pdf),
    )
    try:
        raw = od_uploader(payload)
    except Exception as e:
        log.exception("filer: OD upload raised")
        return FileResult(success=False, file_name=fname, error=f"upload_raised:{e}")

    return _parse_uploader_response(raw, fname)


# ---------------------------------------------------------------------------
# PDF page extraction
# ---------------------------------------------------------------------------

def _extract_pages_to_pdf(source_pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    """Build a new PDF containing only the requested pages, in order."""
    import pymupdf  # type: ignore[import-not-found]

    src = pymupdf.open(stream=source_pdf_bytes, filetype="pdf")
    try:
        out = pymupdf.open()
        try:
            for idx in page_indices:
                if not (0 <= idx < len(src)):
                    raise IndexError(f"page {idx} out of range (len={len(src)})")
                out.insert_pdf(src, from_page=idx, to_page=idx)
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        finally:
            out.close()
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Filename
# ---------------------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _make_file_name(hint: Optional[str], pat_num: int) -> str:
    """Generate a filename for the document.

    If a hint is provided (e.g., 'consent.pdf'), sanitize it. Otherwise build
    'INTAKE_<patnum>_<YYYYMMDDHHMMSS>.pdf'. Always ends in .pdf.
    """
    if hint:
        cleaned = _FILENAME_SAFE_RE.sub("_", hint).strip("_")
        if not cleaned.lower().endswith(".pdf"):
            cleaned = f"{cleaned}.pdf"
        if cleaned:
            return cleaned
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"INTAKE_{pat_num}_{ts}.pdf"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_uploader_response(raw: Any, fname: str) -> FileResult:
    """OD's POST /documents responds with the new document record. Tolerate
    multiple shapes."""
    if raw is None:
        return FileResult(success=False, file_name=fname, error="empty_response")

    # If the wrapper returned an error envelope.
    if isinstance(raw, dict):
        # Some MCP-side helpers wrap as {success: False, error: ...}.
        if raw.get("success") is False:
            return FileResult(
                success=False, file_name=fname,
                error=str(raw.get("error") or "od_returned_error"),
            )
        # Some wrap success as {success: True, document: {...}}.
        doc = raw.get("document") if "document" in raw else raw
        if isinstance(doc, dict):
            doc_num = doc.get("DocNum") or doc.get("documentId") or doc.get("id")
            file_path = doc.get("FilePath") or doc.get("file_path")
            try:
                return FileResult(
                    success=True,
                    doc_num=int(doc_num) if doc_num is not None else None,
                    file_name=doc.get("FileName") or fname,
                    file_path=file_path,
                )
            except (TypeError, ValueError):
                return FileResult(
                    success=False, file_name=fname,
                    error=f"bad_doc_num:{doc_num!r}",
                )

    return FileResult(
        success=False, file_name=fname,
        error=f"unexpected_response_shape:{type(raw).__name__}",
    )
