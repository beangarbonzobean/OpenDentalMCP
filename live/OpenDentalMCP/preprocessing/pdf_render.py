"""
PDF -> PNG page rendering for the local VLM OCR backend.

Local OCR engines (Ollama-served vision models) accept image inputs only,
so PDFs must be rendered to images before being sent. Haiku takes PDFs
natively as `document` content blocks, so this module is only used by the
local backend.

Returns a list of PNG byte strings, one per page. Caller drives the per-page
loop (and concatenates the OCR results).
"""

from __future__ import annotations

import io
import logging
from typing import List

log = logging.getLogger(__name__)


DEFAULT_DPI = 150
MAX_PAGES = 50  # safety cap; see ocr_helper for the operational limit


class PdfRenderError(RuntimeError):
    """Raised when a PDF cannot be opened or rendered."""


def render_pdf_pages(file_bytes: bytes, *, dpi: int = DEFAULT_DPI, max_pages: int = MAX_PAGES) -> List[bytes]:
    """Render every page of a PDF to PNG bytes.

    Returns a list ordered by page number. Each entry is the PNG-encoded
    image of one page.

    Raises PdfRenderError if the PDF can't be opened. Caller decides how to
    handle that (typically: mark the doc Status='error' and skip).
    """
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise PdfRenderError(f"pymupdf not installed: {e}") from e

    try:
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        raise PdfRenderError(f"failed to open pdf: {e}") from e

    try:
        page_count = len(doc)
        if page_count == 0:
            raise PdfRenderError("pdf has zero pages")
        out: list[bytes] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                log.warning("pdf has %d pages; truncating to first %d", page_count, max_pages)
                break
            try:
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                out.append(pix.tobytes("png"))
            except Exception as e:
                raise PdfRenderError(f"failed to render page {i + 1}: {e}") from e
        return out
    finally:
        doc.close()


def render_pdf_first_page(file_bytes: bytes, *, dpi: int = DEFAULT_DPI) -> bytes:
    """Render only the first page. Convenience wrapper for prompt-engineering tests."""
    pages = render_pdf_pages(file_bytes, dpi=dpi, max_pages=1)
    if not pages:
        raise PdfRenderError("no pages rendered")
    return pages[0]
