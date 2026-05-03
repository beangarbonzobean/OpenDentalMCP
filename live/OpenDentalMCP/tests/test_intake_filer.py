"""Tests for preprocessing.intake.filer."""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest

from preprocessing.intake import filer as fl


def _make_pdf(pages: int = 3) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _capture_uploader():
    """Build an uploader callable that captures payloads and returns a canned response."""
    captured: list[dict] = []

    def call(payload: dict) -> dict:
        captured.append(payload)
        return {"DocNum": 99999, "FileName": payload.get("file_name"),
                "FilePath": r"\\SERVER12\OpenDentImages\Y\YoungBen42\foo.pdf"}

    call.captured = captured  # type: ignore[attr-defined]
    return call


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_files_one_doc() -> None:
    pdf = _make_pdf(5)
    uploader = _capture_uploader()
    res = fl.file_document(
        source_pdf_bytes=pdf,
        page_indices=[1, 2, 3],
        pat_num=42,
        def_num=455,
        description="Intake consent",
        od_uploader=uploader,
    )
    assert res.success is True
    assert res.doc_num == 99999
    assert res.file_name and res.file_name.endswith(".pdf")
    assert res.file_path is not None

    # The uploader saw the right payload.
    assert len(uploader.captured) == 1  # type: ignore[attr-defined]
    p = uploader.captured[0]  # type: ignore[attr-defined]
    assert p["patient_id"] == 42
    assert p["category"] == 455
    assert p["description"] == "Intake consent"

    # The uploaded file is a base64-encoded PDF.
    decoded = base64.b64decode(p["file_data"])
    assert decoded[:5] == b"%PDF-"


def test_extracted_pdf_has_only_requested_pages() -> None:
    pdf = _make_pdf(10)
    uploader = _capture_uploader()
    fl.file_document(
        source_pdf_bytes=pdf, page_indices=[2, 5, 7],
        pat_num=42, def_num=455, od_uploader=uploader,
    )
    payload = uploader.captured[0]  # type: ignore[attr-defined]
    decoded = base64.b64decode(payload["file_data"])

    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(decoded))
    assert len(reader.pages) == 3


def test_filename_hint_sanitized() -> None:
    pdf = _make_pdf(2)
    uploader = _capture_uploader()
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138,
        file_name_hint="patient info / form (final)",
        od_uploader=uploader,
    )
    assert res.success is True
    # Spaces, slashes, parens replaced
    assert " " not in (res.file_name or "")
    assert "/" not in (res.file_name or "")
    assert (res.file_name or "").endswith(".pdf")


def test_no_filename_hint_generates_one() -> None:
    pdf = _make_pdf(1)
    uploader = _capture_uploader()
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=uploader,
    )
    assert res.success is True
    # Format: INTAKE_<pat>_<timestamp>.pdf
    assert (res.file_name or "").startswith("INTAKE_42_")
    assert (res.file_name or "").endswith(".pdf")


# ---------------------------------------------------------------------------
# Validation / error paths
# ---------------------------------------------------------------------------

def test_empty_source_pdf_rejected() -> None:
    res = fl.file_document(
        source_pdf_bytes=b"", page_indices=[0],
        pat_num=42, def_num=138, od_uploader=lambda p: {},
    )
    assert res.success is False
    assert res.error == "source_pdf_empty"


def test_empty_page_indices_rejected() -> None:
    pdf = _make_pdf(3)
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[],
        pat_num=42, def_num=138, od_uploader=lambda p: {},
    )
    assert res.success is False
    assert res.error == "no_page_indices"


def test_invalid_pat_or_def_num_rejected() -> None:
    pdf = _make_pdf(1)
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num="not a number", def_num=138,  # type: ignore[arg-type]
        od_uploader=lambda p: {},
    )
    assert res.success is False
    assert res.error == "invalid_pat_or_def_num"


def test_out_of_range_page_index_returns_error() -> None:
    pdf = _make_pdf(3)
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0, 5],  # 5 doesn't exist
        pat_num=42, def_num=138, od_uploader=lambda p: {},
    )
    assert res.success is False
    assert "page_extract_failed" in (res.error or "")


def test_corrupt_source_pdf_returns_error() -> None:
    res = fl.file_document(
        source_pdf_bytes=b"not a pdf at all",
        page_indices=[0], pat_num=42, def_num=138,
        od_uploader=lambda p: {},
    )
    assert res.success is False
    assert "page_extract_failed" in (res.error or "")


def test_uploader_raising_exception_returns_error() -> None:
    pdf = _make_pdf(1)

    def boom(payload):
        raise ConnectionError("OD API down")

    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=boom,
    )
    assert res.success is False
    assert "upload_raised" in (res.error or "")
    assert res.file_name is not None  # we still computed one


def test_uploader_returning_error_envelope() -> None:
    pdf = _make_pdf(1)

    def err_resp(payload):
        return {"success": False, "error": "patient not found"}

    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=err_resp,
    )
    assert res.success is False
    assert "patient not found" in (res.error or "")


def test_uploader_returning_unexpected_shape() -> None:
    pdf = _make_pdf(1)

    def weird(payload):
        return "just a string"

    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=weird,
    )
    assert res.success is False
    assert "unexpected_response_shape" in (res.error or "")


def test_uploader_returning_none() -> None:
    pdf = _make_pdf(1)
    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=lambda p: None,
    )
    assert res.success is False
    assert res.error == "empty_response"


def test_uploader_response_with_document_wrapper() -> None:
    pdf = _make_pdf(1)

    def wrapped(payload):
        return {"success": True, "document": {"DocNum": 11111}}

    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=wrapped,
    )
    assert res.success is True
    assert res.doc_num == 11111


def test_uploader_response_with_alternate_id_field() -> None:
    pdf = _make_pdf(1)

    def alt(payload):
        return {"id": 22222}

    res = fl.file_document(
        source_pdf_bytes=pdf, page_indices=[0],
        pat_num=42, def_num=138, od_uploader=alt,
    )
    assert res.success is True
    assert res.doc_num == 22222


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------

def test_make_file_name_appends_pdf_extension() -> None:
    assert fl._make_file_name("consent", 42).endswith(".pdf")
    assert fl._make_file_name("consent.pdf", 42).endswith(".pdf")


def test_make_file_name_strips_unsafe_chars() -> None:
    out = fl._make_file_name("a/b/c d:e?", 42)
    assert "/" not in out
    assert " " not in out
    assert ":" not in out
    assert "?" not in out


def test_make_file_name_default_format() -> None:
    out = fl._make_file_name(None, 42)
    assert out.startswith("INTAKE_42_")
    assert out.endswith(".pdf")
