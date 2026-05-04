"""Tests for preprocessing.pdf_render."""

from __future__ import annotations

import io

import pytest

from preprocessing.pdf_render import (
    PdfRenderError,
    render_pdf_first_page,
    render_pdf_pages,
)


def _make_pdf_bytes(num_pages: int = 1, width: int = 200, height: int = 200) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_render_single_page_pdf() -> None:
    pdf = _make_pdf_bytes(num_pages=1)
    pages = render_pdf_pages(pdf, dpi=72)
    assert len(pages) == 1
    # PNG magic bytes
    assert pages[0][:8] == b"\x89PNG\r\n\x1a\n"


def test_render_multi_page_pdf() -> None:
    pdf = _make_pdf_bytes(num_pages=4)
    pages = render_pdf_pages(pdf, dpi=72)
    assert len(pages) == 4
    for p in pages:
        assert p[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_max_pages_cap() -> None:
    pdf = _make_pdf_bytes(num_pages=10)
    pages = render_pdf_pages(pdf, dpi=72, max_pages=3)
    assert len(pages) == 3


def test_render_invalid_pdf_raises() -> None:
    with pytest.raises(PdfRenderError):
        render_pdf_pages(b"not a pdf at all", dpi=72)


def test_render_first_page() -> None:
    pdf = _make_pdf_bytes(num_pages=3)
    page1 = render_pdf_first_page(pdf, dpi=72)
    assert page1[:8] == b"\x89PNG\r\n\x1a\n"


def test_dpi_affects_image_size() -> None:
    pdf = _make_pdf_bytes(num_pages=1, width=200, height=200)
    page_72 = render_pdf_first_page(pdf, dpi=72)
    page_300 = render_pdf_first_page(pdf, dpi=300)
    # Higher DPI -> larger image bytes
    assert len(page_300) > len(page_72)
