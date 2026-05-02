"""Tests for preprocessing.document_text_index — orchestration with fakes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from preprocessing import document_text_cache as cache
from preprocessing import document_text_index as idx
from preprocessing import ocr_helper

from tests.conftest import FakeTools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(
    doc_num: int = 1,
    pat_num: int = 100,
    file_name: str = "consent.jpg",
    category: int = 5,
    lname: str = "Young",
    fname: str = "Ben",
    date: str = "2026-04-01",
) -> dict:
    return {
        "DocNum": doc_num,
        "PatNum": pat_num,
        "FileName": file_name,
        "DateCreated": date,
        "DocCategory": category,
        "LName": lname,
        "FName": fname,
    }


def _write_doc_on_share(
    share_root: Path, lname: str, fname: str, pat_num: int, file_name: str, content: bytes,
) -> Path:
    folder = share_root / lname[0].upper() / f"{lname}{fname}{pat_num}"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / file_name
    p.write_bytes(content)
    return p


def _make_ocr_fn(text: str = "Hello world", *, cost: float = 0.001, unreadable: bool = False):
    """Return a stub callable mimicking ocr_helper.ocr_bytes."""
    calls = []

    def fn(file_bytes: bytes, *, media_type: str, **kwargs):
        calls.append((len(file_bytes), media_type))
        return ocr_helper.OcrResult(
            text="" if unreadable else text,
            model="claude-haiku-4-5-20251001",
            input_tokens=100,
            output_tokens=50,
            cost_usd=cost,
            media_type=media_type,
            is_unreadable=unreadable,
        )

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------

def test_iter_documents_keyset_paginates(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [_doc(doc_num=1), _doc(doc_num=2), _doc(doc_num=3)])
    fake_tools.push_rows("SELECT", [_doc(doc_num=4)])
    fake_tools.push_rows("SELECT", [])  # terminator
    docs = list(idx.iter_documents(fake_tools, batch=3))
    assert [d.DocNum for d in docs] == [1, 2, 3, 4]
    # Cursor should advance after first batch -> the second query references
    # the last doc_num. Easiest assertion: the last query string contains '> 3'
    # (rendered by our int interpolator).
    assert " > 3 " in fake_tools.queries[1]


def test_iter_documents_handles_empty_first_page(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [])
    assert list(idx.iter_documents(fake_tools)) == []


def test_iter_documents_propagates_db_failure(fake_tools: FakeTools) -> None:
    fake_tools.fail("SELECT", "connection refused")
    with pytest.raises(RuntimeError):
        list(idx.iter_documents(fake_tools))


def test_query_rejects_non_int_params(fake_tools: FakeTools) -> None:
    with pytest.raises(ValueError):
        idx._query(fake_tools, "SELECT * FROM document WHERE DocNum > ?", ("oops",))  # type: ignore[arg-type]


def test_count_documents_for_patient(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [{"n": 7}])
    assert idx.count_documents_for_patient(fake_tools, 100) == 7


def test_all_doc_nums(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [{"DocNum": 1}, {"DocNum": 2}, {"DocNum": 3}])
    assert idx.all_doc_nums(fake_tools) == {1, 2, 3}


# ---------------------------------------------------------------------------
# ocr_one_document — happy and edge cases
# ---------------------------------------------------------------------------

def test_ocr_one_document_happy_path(share_root: Path) -> None:
    doc_path = _write_doc_on_share(share_root, "Young", "Ben", 42, "consent.jpg", b"\x89PNGfake")
    doc = idx.OdDocRow(
        DocNum=42, PatNum=42, FileName="consent.jpg",
        DateCreated="2026-04-01", DocCategory=5, LName="Young", FName="Ben",
    )
    fn = _make_ocr_fn(text="patient agrees")
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert row.Text == "patient agrees"
    assert row.PageCount == 1
    assert row.Sha256 is not None
    assert row.CostUsd == 0.001
    assert len(fn.calls) == 1  # type: ignore[attr-defined]


def test_ocr_one_document_unreadable_marks_status(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "Jane", 7, "blurry.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="blurry.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="Jane",
    )
    fn = _make_ocr_fn(unreadable=True)
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "unreadable"
    assert row.Text == ""


def test_ocr_one_document_unsupported_extension_no_api_call(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "Jane", 7, "xray.dcm", b"DICM")
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="xray.dcm",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="Jane",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "unsupported"
    assert "extension" in (row.ErrorMessage or "")
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_skip_category_no_api_call(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "Jane", 7, "scan.jpg", b"x")
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="scan.jpg",
        DateCreated="2026-04-01", DocCategory=999, LName="Doe", FName="Jane",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root, skip_categories={999})
    assert row.Status == "unsupported"
    assert row.ErrorMessage == "category_skipped"
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_missing_file(share_root: Path) -> None:
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="ghost.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="Jane",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "error"
    assert "not_found" in (row.ErrorMessage or "")
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_oversize_file(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "Jane", 7, "big.jpg", b"x" * 100)
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="big.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="Jane",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root, max_file_bytes=10)
    assert row.Status == "unsupported"
    assert "oversize" in (row.ErrorMessage or "")
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_missing_lname(share_root: Path) -> None:
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="", FName="J",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "error"
    assert "missing_lname" in (row.ErrorMessage or "")
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_missing_filename() -> None:
    doc = idx.OdDocRow(
        DocNum=7, PatNum=7, FileName=None,
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn)
    assert row.Status == "unsupported"
    assert row.ErrorMessage == "no_filename"


def test_ocr_one_document_ocr_config_error(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "J", 1, "x.jpg", b"x")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )

    def boom(file_bytes: bytes, *, media_type: str, **kw):
        raise ocr_helper.OcrConfigError("ANTHROPIC_API_KEY not set")

    row = idx.ocr_one_document(doc, ocr_fn=boom, share_root=share_root)
    assert row.Status == "error"
    assert "config" in (row.ErrorMessage or "")


def test_ocr_one_document_ocr_runtime_error(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "J", 1, "x.jpg", b"x")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )

    def boom(file_bytes: bytes, *, media_type: str, **kw):
        raise ocr_helper.OcrError("network")

    row = idx.ocr_one_document(doc, ocr_fn=boom, share_root=share_root)
    assert row.Status == "error"
    assert "ocr_failed" in (row.ErrorMessage or "")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _setup_three_docs(fake_tools: FakeTools, share_root: Path) -> None:
    fake_tools.push_rows("SELECT", [
        _doc(doc_num=1, pat_num=10),
        _doc(doc_num=2, pat_num=10),
        _doc(doc_num=3, pat_num=11),
    ])
    fake_tools.push_rows("SELECT", [])
    for n in (1, 2, 3):
        _write_doc_on_share(share_root, "Young", "Ben", 10 if n != 3 else 11, _doc(doc_num=n)["FileName"], b"x")


def test_backfill_happy_path(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    _setup_three_docs(fake_tools, share_root)
    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools,
        cache_path=cache_path,
        lock_path=lock_path,
        max_docs=10,
        max_spend_usd=1.0,
        ocr_fn=fn,
        share_root=share_root,
    )
    assert res.success is True
    assert res.scanned == 3
    assert res.ocrd == 3
    assert res.errors == 0
    assert res.skipped_unsupported == 0
    assert res.cost_usd_estimate == pytest.approx(0.003)
    assert res.halted_reason is None
    assert len(fn.calls) == 3  # type: ignore[attr-defined]


def test_backfill_max_docs_halts(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    _setup_three_docs(fake_tools, share_root)
    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=2, max_spend_usd=10.0, ocr_fn=fn, share_root=share_root,
    )
    assert res.scanned == 2
    assert res.ocrd == 2
    assert res.halted_reason == "max_docs"


def test_backfill_budget_halts(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    _setup_three_docs(fake_tools, share_root)
    # cost per doc = 0.001 → after 2 docs we're at 0.002, on the 3rd doc we
    # check the budget BEFORE OCR, so budget=0.0015 halts before the 3rd OCR.
    fn = _make_ocr_fn(cost=0.001)
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=0.0015, ocr_fn=fn, share_root=share_root,
    )
    assert res.ocrd == 2
    assert res.halted_reason == "budget"
    assert len(fn.calls) == 2  # type: ignore[attr-defined]


def test_backfill_retries_error_rows(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """An 'error' row from a prior failed run should be retried, not skipped."""
    cache.init_cache(cache_path)
    with cache.open_cache(cache_path) as conn:
        # Pre-seed an error row for DocNum=1.
        cache.put_text(conn, cache.DocTextRow(
            DocNum=1, PatNum=10, Status="error", ErrorMessage="prior failure",
            OcrAt="2026-04-01",
        ))

    fake_tools.push_rows("SELECT", [_doc(doc_num=1, pat_num=10), _doc(doc_num=2, pat_num=10)])
    fake_tools.push_rows("SELECT", [])
    _write_doc_on_share(share_root, "Young", "Ben", 10, "consent.jpg", b"x")

    fn = _make_ocr_fn(text="recovered")
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, ocr_fn=fn, share_root=share_root,
    )
    # Both docs OCR'd: doc 1 retried (was error), doc 2 fresh.
    assert res.ocrd == 2
    assert res.skipped_cached == 0
    with cache.open_cache(cache_path) as conn:
        r1 = cache.get_text(conn, 1)
        assert r1 is not None
        assert r1.Status == "ok"
        assert r1.Text == "recovered"


def test_backfill_idempotent_skips_cached(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    _setup_three_docs(fake_tools, share_root)
    fn = _make_ocr_fn()

    # Run 1
    idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, ocr_fn=fn, share_root=share_root,
    )
    assert len(fn.calls) == 3  # type: ignore[attr-defined]

    # Reset the iterator's row source for run 2.
    fake_tools.push_rows("SELECT", [
        _doc(doc_num=1, pat_num=10),
        _doc(doc_num=2, pat_num=10),
        _doc(doc_num=3, pat_num=11),
    ])
    fake_tools.push_rows("SELECT", [])

    res2 = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, ocr_fn=fn, share_root=share_root,
    )
    assert res2.scanned == 3
    assert res2.ocrd == 0
    assert res2.skipped_cached == 3
    # Still 3 total — no extra OCR calls.
    assert len(fn.calls) == 3  # type: ignore[attr-defined]


def test_backfill_dry_run_does_not_call_ocr_or_write_cache(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    _setup_three_docs(fake_tools, share_root)
    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, ocr_fn=fn, share_root=share_root,
        dry_run=True,
    )
    assert res.scanned == 3
    assert res.ocrd == 0
    assert fn.calls == []  # type: ignore[attr-defined]
    with cache.open_cache(cache_path) as conn:
        assert cache.cached_doc_nums(conn) == set()


def test_backfill_lock_contention(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    # Pre-acquire the lock by opening another instance of the file.
    import portalocker
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Touch lock file
    fh = open(lock_path, "a+")
    portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    try:
        res = idx.backfill(
            fake_tools, cache_path=cache_path, lock_path=lock_path,
            max_docs=1, max_spend_usd=1.0, ocr_fn=_make_ocr_fn(), share_root=share_root,
        )
        assert res.halted_reason == "locked"
    finally:
        portalocker.unlock(fh)
        fh.close()


def test_backfill_prune_orphans(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    # First, populate cache directly with 3 rows (DocNums 1, 2, 99).
    cache.init_cache(cache_path)
    with cache.open_cache(cache_path) as conn:
        for n in (1, 2, 99):
            cache.put_text(conn, cache.DocTextRow(DocNum=n, PatNum=10, Text="x", OcrAt="2026-04-01", Status="ok"))

    # Iter returns no new docs (all "in OD" are 1, 2 only — DocNum 99 is gone).
    fake_tools.push_rows("SELECT", [])
    fake_tools.push_rows("SELECT", [{"DocNum": 1}, {"DocNum": 2}])  # for all_doc_nums

    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, ocr_fn=_make_ocr_fn(), share_root=share_root,
        prune=True,
    )
    assert res.pruned == 1
    with cache.open_cache(cache_path) as conn:
        assert cache.cached_doc_nums(conn) == {1, 2}


# ---------------------------------------------------------------------------
# fetch_or_ocr (single-doc, on-demand)
# ---------------------------------------------------------------------------

def test_fetch_or_ocr_cache_hit(
    fake_tools: FakeTools, share_root: Path, cache_path: Path,
) -> None:
    cache.init_cache(cache_path)
    with cache.open_cache(cache_path) as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=42, PatNum=10, Text="cached text", OcrAt="2026-04-01", Status="ok",
        ))
    fn = _make_ocr_fn()
    row, source = idx.fetch_or_ocr(
        fake_tools, 42, cache_path=cache_path, ocr_fn=fn, share_root=share_root,
    )
    assert source == "cache"
    assert row is not None
    assert row.Text == "cached text"
    assert fn.calls == []  # type: ignore[attr-defined]


def test_fetch_or_ocr_cache_miss_triggers_ocr(
    fake_tools: FakeTools, share_root: Path, cache_path: Path,
) -> None:
    fake_tools.push_rows("SELECT", [_doc(doc_num=42, pat_num=10)])
    _write_doc_on_share(share_root, "Young", "Ben", 10, "consent.jpg", b"x")
    fn = _make_ocr_fn(text="fresh ocr")
    row, source = idx.fetch_or_ocr(
        fake_tools, 42, cache_path=cache_path, ocr_fn=fn, share_root=share_root,
    )
    assert source == "fresh"
    assert row is not None
    assert row.Text == "fresh ocr"
    assert len(fn.calls) == 1  # type: ignore[attr-defined]
    # Now cached.
    with cache.open_cache(cache_path) as conn:
        cached = cache.get_text(conn, 42)
        assert cached is not None
        assert cached.Text == "fresh ocr"


def test_fetch_or_ocr_missing_in_od(
    fake_tools: FakeTools, share_root: Path, cache_path: Path,
) -> None:
    fake_tools.push_rows("SELECT", [])  # no row in OD
    row, source = idx.fetch_or_ocr(
        fake_tools, 999, cache_path=cache_path, ocr_fn=_make_ocr_fn(), share_root=share_root,
    )
    assert source == "missing"
    assert row is None


def test_fetch_or_ocr_re_ocrs_when_previously_errored(
    fake_tools: FakeTools, share_root: Path, cache_path: Path,
) -> None:
    cache.init_cache(cache_path)
    with cache.open_cache(cache_path) as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=42, PatNum=10, Text="", Status="error",
            OcrAt="2026-04-01", ErrorMessage="prior failure",
        ))
    fake_tools.push_rows("SELECT", [_doc(doc_num=42, pat_num=10)])
    _write_doc_on_share(share_root, "Young", "Ben", 10, "consent.jpg", b"x")
    fn = _make_ocr_fn(text="recovered")
    row, source = idx.fetch_or_ocr(
        fake_tools, 42, cache_path=cache_path, ocr_fn=fn, share_root=share_root,
    )
    assert source == "fresh"
    assert row is not None
    assert row.Text == "recovered"
