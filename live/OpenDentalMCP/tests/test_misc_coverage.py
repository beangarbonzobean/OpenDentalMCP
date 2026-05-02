"""Targeted edge-case tests to push preprocessing/ coverage past ~92%.

Covers:
  - DOC_TEXT_SKIP_CATEGORIES env-var parsing variants
  - PDF page-count helper happy path and error path
  - Backfill error / exception path
  - ocr_helper._status_from_error fallback paths
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preprocessing import document_text_index as idx
from preprocessing import ocr_helper
from preprocessing import preflight

from tests.conftest import FakeTools


# ---------------------------------------------------------------------------
# DOC_TEXT_SKIP_CATEGORIES parsing
# ---------------------------------------------------------------------------

def test_skip_categories_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_TEXT_SKIP_CATEGORIES", raising=False)
    assert idx._skip_categories() == set()


def test_skip_categories_comma_and_semicolon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_TEXT_SKIP_CATEGORIES", "1,2 ; 3, 4 ; ")
    assert idx._skip_categories() == {1, 2, 3, 4}


def test_skip_categories_ignores_bad_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_TEXT_SKIP_CATEGORIES", "1, oops, 3")
    assert idx._skip_categories() == {1, 3}


# ---------------------------------------------------------------------------
# PDF page count
# ---------------------------------------------------------------------------

def test_pdf_page_count_happy() -> None:
    # Build a tiny in-memory PDF via pypdf.
    pypdf = pytest.importorskip("pypdf")
    import io
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    pages = idx._pdf_page_count(buf.getvalue())
    assert pages == 3


def test_pdf_page_count_corrupt_returns_none() -> None:
    assert idx._pdf_page_count(b"not a pdf") is None


def test_ocr_one_document_pdf_too_many_pages(share_root: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    import io
    writer = pypdf.PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    folder = share_root / "D" / "DoeJane7"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "eob.pdf").write_bytes(pdf_bytes)

    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="eob.pdf",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="Jane",
    )
    row = idx.ocr_one_document(doc, share_root=share_root, max_pdf_pages=2)
    assert row.Status == "unsupported"
    assert "too_many_pages" in (row.ErrorMessage or "")


def test_ocr_one_document_empty_file(share_root: Path) -> None:
    folder = share_root / "D" / "DoeJ7"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "x.jpg").write_bytes(b"")
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    row = idx.ocr_one_document(doc, share_root=share_root)
    assert row.Status == "error"
    assert row.ErrorMessage == "empty_file"


# ---------------------------------------------------------------------------
# backfill error / lock release
# ---------------------------------------------------------------------------

def test_backfill_handles_iter_failure(
    fake_tools: FakeTools, tmp_path: Path,
) -> None:
    fake_tools.fail("SELECT", "db down")
    res = idx.backfill(
        fake_tools,
        cache_path=tmp_path / "cache.db",
        lock_path=tmp_path / ".lock",
        max_docs=10,
    )
    assert res.success is False
    assert res.halted_reason == "error"
    assert "db down" in (res.error or "")


def test_release_lock_with_none_is_safe() -> None:
    # No exception expected
    idx._release_lock(None)


# ---------------------------------------------------------------------------
# ocr_helper._status_from_error variants
# ---------------------------------------------------------------------------

class _ErrWithResponse(Exception):
    def __init__(self):
        super().__init__("boom")
        class _R:
            status_code = 502
        self.response = _R()


class _ErrWithStatus(Exception):
    def __init__(self):
        super().__init__("boom")
        self.status = 504


def test_status_from_error_uses_response_status_code() -> None:
    assert ocr_helper._status_from_error(_ErrWithResponse()) == 502


def test_status_from_error_uses_status_attr() -> None:
    assert ocr_helper._status_from_error(_ErrWithStatus()) == 504


def test_status_from_error_returns_none_for_unknown() -> None:
    assert ocr_helper._status_from_error(RuntimeError("boom")) is None


# ---------------------------------------------------------------------------
# preflight: failure paths in cache/disk checks
# ---------------------------------------------------------------------------

def test_check_cache_fails_when_init_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from preprocessing import document_text_cache as dtc
    def boom(path=None):
        raise RuntimeError("disk full")
    monkeypatch.setattr(dtc, "init_cache", boom)
    r = preflight._check_cache_opens()
    assert r.ok is False
    assert "disk full" in r.detail


def test_check_disk_space_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    def boom(path):
        raise OSError("no such directory")
    monkeypatch.setattr(shutil, "disk_usage", boom)
    r = preflight._check_disk_space()
    assert r.ok is False


def test_enumerate_doc_categories_failure(fake_tools: FakeTools) -> None:
    fake_tools.fail("SELECT", "perm denied")
    chk, cats = preflight._enumerate_doc_categories(fake_tools)
    assert chk.ok is False
    assert cats == []


def test_share_root_check_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    nope = tmp_path / "definitely_does_not_exist"
    monkeypatch.setenv("OD_DOC_ROOT", str(nope))
    r = preflight._check_share_root_exists()
    assert r.ok is False
