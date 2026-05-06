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
    # Tag the bytes with the (PatNum, FileName) tuple so each test doc gets a
    # distinct SHA-256 — otherwise the dedup fast-path in ocr_one_document
    # short-circuits the second+ identical-content doc, which is correct
    # production behavior but defeats tests that expect every doc to be OCR'd.
    # Tests that explicitly want to exercise dedup should write content directly
    # via Path.write_bytes(), not through this helper.
    uniqued = content + f"|tag:{pat_num}:{file_name}".encode("utf-8")
    p.write_bytes(uniqued)
    return p


def _make_ocr_fn(
    text: str = "Hello world from the OCR test stub",  # >= MIN_OK_CHARS (20)
    *, cost: float = 0.001, unreadable: bool = False,
):
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
    fn = _make_ocr_fn(text="patient agrees to the consent form")
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert row.Text == "patient agrees to the consent form"
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


def test_ocr_one_document_html_extracted_no_ocr(share_root: Path) -> None:
    """HTML files (Dentrix Eligibility reports etc.) should be parsed
    locally — no OCR call, no cost, status='ok'."""
    html_bytes = (
        b"<html><body><div>Subscriber: John Doe</div>"
        b"<div>Group: 12345</div></body></html>"
    )
    _write_doc_on_share(share_root, "Doe", "J", 1, "Page1.htm", html_bytes)
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="Page1.htm",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn()  # should NOT be invoked
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert "Subscriber: John Doe" in row.Text
    assert "Group: 12345" in row.Text
    assert row.OcrModel == "html_extract"
    assert row.CostUsd == 0.0
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_html_empty_marks_unreadable(share_root: Path) -> None:
    """An HTML file that produces no extractable text marks unreadable, not error."""
    _write_doc_on_share(share_root, "Doe", "J", 1, "blank.htm", b"<html><body></body></html>")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="blank.htm",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    row = idx.ocr_one_document(doc, share_root=share_root)
    assert row.Status == "unreadable"
    assert row.OcrModel == "html_extract"


def test_ocr_one_document_tmp_artifact_terminal(share_root: Path) -> None:
    """tmp*.tmp.png files should be marked unsupported (terminal) without
    even reading the file — they're never going to OCR usefully."""
    # File doesn't even need to exist on the share — we short-circuit on name.
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="tmpA282.tmp.png",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn()
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "unsupported"
    assert row.ErrorMessage == "tmp_artifact"
    assert fn.calls == []  # type: ignore[attr-defined]


def test_ocr_one_document_tmp_artifact_with_prefix(share_root: Path) -> None:
    """tmp artifacts may have a numeric prefix added by Dexis (e.g. '325_tmp...')."""
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="325_tmpA282.tmp.png",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    row = idx.ocr_one_document(doc, share_root=share_root)
    assert row.Status == "unsupported"
    assert row.ErrorMessage == "tmp_artifact"


def test_ocr_one_document_short_output_demoted_to_unreadable(share_root: Path) -> None:
    """A model that returned <MIN_OK_CHARS without saying UNREADABLE used to
    cache as Status='ok' with empty/garbage Text. Now it's demoted."""
    _write_doc_on_share(share_root, "Doe", "J", 1, "blank.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="blank.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn(text="abc")  # 3 chars, not the UNREADABLE sentinel
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "unreadable"
    assert row.Text == ""  # not indexed
    assert "short_output" in (row.ErrorMessage or "")
    assert "3" in (row.ErrorMessage or "")


def test_ocr_one_document_meaningful_text_kept_as_ok(share_root: Path) -> None:
    _write_doc_on_share(share_root, "Doe", "J", 1, "form.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="form.jpg",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn(text="patient signed the consent form today")  # > 20 chars
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert row.Text.startswith("patient signed")
    assert row.ErrorMessage is None


def test_ocr_one_document_html_short_output_demoted(share_root: Path) -> None:
    """Same floor applies to html_extract — guards against the UTF-16 BOM bug
    where the decoder emits a few stray characters."""
    _write_doc_on_share(share_root, "Doe", "J", 1, "tiny.htm",
                        b"<html><body>x</body></html>")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="tiny.htm",
        DateCreated="2026-04-01", DocCategory=1, LName="Doe", FName="J",
    )
    row = idx.ocr_one_document(doc, share_root=share_root)
    assert row.Status == "unreadable"
    assert row.OcrModel == "html_extract"
    assert row.Text == ""
    assert "short_output" in (row.ErrorMessage or "")


def test_ocr_one_document_dedup_lookup_hit_skips_ocr(share_root: Path) -> None:
    """When dedup_lookup returns a prior ok row, OCR is NOT called and the
    text is reused."""
    _write_doc_on_share(share_root, "Doe", "J", 99, "form.pdf", b"%PDF-fake")
    doc = idx.OdDocRow(
        DocNum=2, PatNum=99, FileName="form.pdf",
        DateCreated="2026-04-01", DocCategory=5, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn()  # would-be OCR — should not be called
    prior = cache.DocTextRow(
        DocNum=1, PatNum=42, FileName="form.pdf",
        Text="cached text from doc 1 reused for doc 2",
        Sha256="abc", OcrModel="qwen2.5vl:7b", OcrAt="2026-05-04",
        Status="ok", PageCount=1,
    )
    captured = []
    def lookup(sha):
        captured.append(sha)
        return prior
    row = idx.ocr_one_document(
        doc, ocr_fn=fn, share_root=share_root, dedup_lookup=lookup,
    )
    assert row.Status == "ok"
    assert row.Text == "cached text from doc 1 reused for doc 2"
    assert row.OcrModel.startswith("dedup:")
    assert "qwen2.5vl:7b" in row.OcrModel  # source model preserved
    assert row.CostUsd == 0.0
    assert row.Sha256  # was computed
    assert row.PatNum == 99  # row points to NEW doc, not source
    assert row.DocNum == 2
    assert fn.calls == []  # type: ignore[attr-defined]
    assert len(captured) == 1  # lookup was queried with the SHA


def test_ocr_one_document_dedup_lookup_miss_proceeds_to_ocr(share_root: Path) -> None:
    """When dedup_lookup returns None, normal OCR happens."""
    _write_doc_on_share(share_root, "Doe", "J", 99, "form.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=2, PatNum=99, FileName="form.jpg",
        DateCreated="2026-04-01", DocCategory=5, LName="Doe", FName="J",
    )
    fn = _make_ocr_fn(text="freshly OCR'd content for the new patient")
    row = idx.ocr_one_document(
        doc, ocr_fn=fn, share_root=share_root,
        dedup_lookup=lambda sha: None,
    )
    assert row.Status == "ok"
    assert row.Text == "freshly OCR'd content for the new patient"
    assert "dedup" not in (row.OcrModel or "")
    assert len(fn.calls) == 1  # type: ignore[attr-defined]


def test_ocr_one_document_dedup_does_not_self_match(share_root: Path) -> None:
    """If somehow lookup returns the SAME DocNum (shouldn't happen in practice
    since we only call lookup before writing), don't loop."""
    _write_doc_on_share(share_root, "Doe", "J", 99, "form.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=42, PatNum=99, FileName="form.jpg",
        DateCreated="2026-04-01", DocCategory=5, LName="Doe", FName="J",
    )
    self_row = cache.DocTextRow(
        DocNum=42, PatNum=99, FileName="form.jpg",
        Text="my own old text", Sha256="abc", OcrModel="x",
        OcrAt="2026-04-01", Status="ok", PageCount=1,
    )
    fn = _make_ocr_fn(text="newly OCR'd text for this same doc")
    row = idx.ocr_one_document(
        doc, ocr_fn=fn, share_root=share_root,
        dedup_lookup=lambda sha: self_row,
    )
    # Should fall through to OCR
    assert "dedup" not in (row.OcrModel or "")
    assert row.Text.startswith("newly OCR'd")


def test_ocr_one_document_passes_category_prompt(
    share_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If OCR_CATEGORY_PROMPTS_FILE has an entry for this DocCategory, the
    custom prompt flows into the ocr_fn call."""
    prompts_file = tmp_path / "prompts.json"
    prompts_file.write_text(
        '{"461": "This is an EOB. Preserve patient name, claim numbers, '
        'CDT codes, allowed amounts."}'
    )
    monkeypatch.setenv("OCR_CATEGORY_PROMPTS_FILE", str(prompts_file))
    ocr_helper.reset_category_prompts_cache()

    _write_doc_on_share(share_root, "Doe", "J", 1, "eob.pdf", b"%PDF-fake")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="eob.pdf",
        DateCreated="2026-04-01", DocCategory=461,  # match the prompt key
        LName="Doe", FName="J",
    )
    captured_prompts: list = []
    def fn(file_bytes: bytes, *, media_type: str, prompt: Optional[str] = None, **kw):
        captured_prompts.append(prompt)
        return ocr_helper.OcrResult(
            text="EOB text long enough to satisfy the floor",
            model="qwen2.5vl:7b", input_tokens=10, output_tokens=10,
            cost_usd=0.0, media_type=media_type, is_unreadable=False,
        )
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert len(captured_prompts) == 1
    assert "EOB" in captured_prompts[0]
    assert "CDT codes" in captured_prompts[0]


def test_ocr_one_document_passes_no_prompt_for_unknown_category(
    share_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Unknown category: ocr_fn called WITHOUT prompt (so default applies)."""
    prompts_file = tmp_path / "prompts.json"
    prompts_file.write_text('{"461": "EOB prompt"}')
    monkeypatch.setenv("OCR_CATEGORY_PROMPTS_FILE", str(prompts_file))
    ocr_helper.reset_category_prompts_cache()

    _write_doc_on_share(share_root, "Doe", "J", 1, "x.jpg", b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=1, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=999,  # unknown
        LName="Doe", FName="J",
    )
    seen_kwargs: list = []
    def fn(file_bytes: bytes, *, media_type: str, **kw):
        seen_kwargs.append(kw)
        return ocr_helper.OcrResult(
            text="generic OCR text long enough for floor",
            model="qwen2.5vl:7b", input_tokens=10, output_tokens=10,
            cost_usd=0.0, media_type=media_type, is_unreadable=False,
        )
    idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert len(seen_kwargs) == 1
    # No prompt kwarg passed (we only pass it when category lookup hits).
    assert "prompt" not in seen_kwargs[0]


def test_ocr_one_document_uses_patnum_fallback(share_root: Path) -> None:
    """If the constructed folder doesn't exist, scan for one ending in PatNum."""
    # Folder on disk has the OD-sanitized name (no hyphen).
    folder = share_root / "E" / "EDWARDSGRAYADEHRRA18674"
    folder.mkdir(parents=True)
    (folder / "x.jpg").write_bytes(b"\xff\xd8\xff")
    doc = idx.OdDocRow(
        DocNum=1, PatNum=18674, FileName="x.jpg",
        DateCreated="2026-04-01", DocCategory=1,
        LName="EDWARDS-GRAY", FName="ADEHRRA",
    )
    fn = _make_ocr_fn(text="recovered text from the EDWARDS-GRAY patient folder")
    row = idx.ocr_one_document(doc, ocr_fn=fn, share_root=share_root)
    assert row.Status == "ok"
    assert "EDWARDS-GRAY" in row.Text
    assert len(fn.calls) == 1  # type: ignore[attr-defined]


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
    # Use a distinct filename per DocNum so each row gets unique on-disk bytes
    # (otherwise the dedup fast-path treats docs 1 & 2 as duplicates because
    # they share PatNum and the default file_name).
    rows = [
        _doc(doc_num=1, pat_num=10, file_name="doc1.jpg"),
        _doc(doc_num=2, pat_num=10, file_name="doc2.jpg"),
        _doc(doc_num=3, pat_num=11, file_name="doc3.jpg"),
    ]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(
            share_root, "Young", "Ben",
            r["PatNum"], r["FileName"], b"x",
        )


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

    fn = _make_ocr_fn(text="recovered text for the retry path")
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
        assert r1.Text == "recovered text for the retry path"


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
    fn = _make_ocr_fn(text="fresh ocr from the runtime path")
    row, source = idx.fetch_or_ocr(
        fake_tools, 42, cache_path=cache_path, ocr_fn=fn, share_root=share_root,
    )
    assert source == "fresh"
    assert row is not None
    assert row.Text == "fresh ocr from the runtime path"
    assert len(fn.calls) == 1  # type: ignore[attr-defined]
    # Now cached.
    with cache.open_cache(cache_path) as conn:
        cached = cache.get_text(conn, 42)
        assert cached is not None
        assert cached.Text == "fresh ocr from the runtime path"


def test_fetch_or_ocr_missing_in_od(
    fake_tools: FakeTools, share_root: Path, cache_path: Path,
) -> None:
    fake_tools.push_rows("SELECT", [])  # no row in OD
    row, source = idx.fetch_or_ocr(
        fake_tools, 999, cache_path=cache_path, ocr_fn=_make_ocr_fn(), share_root=share_root,
    )
    assert source == "missing"
    assert row is None


# ---------------------------------------------------------------------------
# Parallel backfill (workers > 1)
# ---------------------------------------------------------------------------

def test_backfill_parallel_processes_all_docs(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """workers=4 should process all docs and end in a consistent state."""
    n_docs = 20
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, n_docs + 1)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    fn = _make_ocr_fn(text="parallel ok output text from the worker pool")
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=100, max_spend_usd=10.0, workers=4,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.success is True
    assert res.scanned == n_docs
    assert res.ocrd == n_docs
    assert res.errors == 0
    assert len(fn.calls) == n_docs  # type: ignore[attr-defined]
    with cache.open_cache(cache_path) as conn:
        nums = cache.cached_doc_nums(conn)
        assert len(nums) == n_docs


def test_backfill_parallel_max_docs_halts(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, 21)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=8, max_spend_usd=10.0, workers=3,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.scanned == 8
    assert res.halted_reason == "max_docs"
    # All in-flight at the time of halt are allowed to finish
    assert res.ocrd <= 8
    assert res.ocrd >= 1


def test_backfill_parallel_budget_halts(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """Budget cap halts the loop. In parallel, in-flight workers may slightly
    overshoot the cap — that's expected and bounded by `workers`."""
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, 21)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    # Each Haiku-fallback page is $0.01; here we pretend every doc costs that.
    fn = _make_ocr_fn(cost=0.01)
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=20, max_spend_usd=0.05, workers=4,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.halted_reason == "budget"
    # Budget triggers once running cost_usd_estimate >= max_spend_usd, i.e.
    # after at least 5 docs at $0.01 each have completed. The backpressure
    # queue depth is workers*2, so up to that many additional in-flight
    # workers may finish after the halt is set. Bound: budget_count + 2*workers.
    assert res.ocrd >= 5
    assert res.ocrd <= 5 + 2 * 4


def test_backfill_parallel_cached_docs_are_skipped(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """A pre-populated cache row means the parallel path skips that DocNum."""
    cache.init_cache(cache_path)
    with cache.open_cache(cache_path) as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=1, PatNum=10, Text="already cached",
            OcrAt="2026-04-01", Status="ok",
        ))
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, 6)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, workers=4,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.skipped_cached == 1
    assert res.ocrd == 4
    assert len(fn.calls) == 4  # type: ignore[attr-defined]


def test_backfill_parallel_dry_run_uses_sequential_path(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """dry_run=True forces sequential, even when workers>1, for deterministic
    log output."""
    rows = [_doc(doc_num=i, pat_num=10) for i in range(1, 6)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, workers=4, dry_run=True,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.scanned == 5
    assert res.ocrd == 0
    assert fn.calls == []  # type: ignore[attr-defined]


def test_backfill_parallel_worker_exception_counts_as_error(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """If the worker function itself raises (not an OcrError but a programmer
    bug), the future fails. The drain code logs it and increments errors."""
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, 6)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    # Make the OCR helper raise an unexpected exception (not OcrError) to bubble
    # past ocr_one_document's catches into the worker and surface in .result().
    def boom_in_thread(*a, **kw):
        raise RuntimeError("simulated worker bug")

    # Patch document_text_cache.put_text to fail half the time so the future
    # raises after ocr_one_document succeeds. This exercises the future-result
    # exception path in _drain_one.
    import preprocessing.document_text_cache as dtc
    real_put = dtc.put_text
    counter = {"n": 0}

    def flaky_put(conn, row):
        counter["n"] += 1
        if counter["n"] % 2 == 0:
            raise RuntimeError("simulated cache write fail")
        return real_put(conn, row)

    import unittest.mock as _mock
    with _mock.patch.object(dtc, "put_text", side_effect=flaky_put):
        fn = _make_ocr_fn(text="ok output text long enough to pass the floor")
        res = idx.backfill(
            fake_tools, cache_path=cache_path, lock_path=lock_path,
            max_docs=10, max_spend_usd=1.0, workers=2,
            ocr_fn=fn, share_root=share_root,
        )
    # Some succeed, some are counted as errors via the drain exception path.
    assert res.success is True
    assert res.scanned == 5
    assert res.ocrd + res.errors == 5
    assert res.errors >= 1


def test_backfill_workers_clamped_to_one_minimum(
    fake_tools: FakeTools, share_root: Path, cache_path: Path, lock_path: Path,
) -> None:
    """workers<=1 (including 0 or negative) routes through the sequential path."""
    rows = [_doc(doc_num=i, pat_num=10, file_name=f"doc{i}.jpg") for i in range(1, 4)]
    fake_tools.push_rows("SELECT", rows)
    fake_tools.push_rows("SELECT", [])
    for r in rows:
        _write_doc_on_share(share_root, "Young", "Ben", 10, r["FileName"], b"x")

    fn = _make_ocr_fn()
    res = idx.backfill(
        fake_tools, cache_path=cache_path, lock_path=lock_path,
        max_docs=10, max_spend_usd=1.0, workers=0,
        ocr_fn=fn, share_root=share_root,
    )
    assert res.ocrd == 3


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
    fn = _make_ocr_fn(text="recovered text after the prior error row")
    row, source = idx.fetch_or_ocr(
        fake_tools, 42, cache_path=cache_path, ocr_fn=fn, share_root=share_root,
    )
    assert source == "fresh"
    assert row is not None
    assert row.Text == "recovered text after the prior error row"
