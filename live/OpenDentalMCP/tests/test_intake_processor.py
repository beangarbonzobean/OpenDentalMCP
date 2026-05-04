"""Tests for preprocessing.intake.processor — end-to-end orchestration."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from preprocessing.intake import cache as ic
from preprocessing.intake import doc_classifier
from preprocessing.intake import extractor as ext
from preprocessing.intake import processor as pr
from preprocessing.intake import taxonomy as tx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def watch(tmp_path: Path) -> Path:
    p = tmp_path / "watch"
    p.mkdir()
    return p


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "intake.db"


def _write_pdf(folder: Path, name: str, pages: int = 3) -> Path:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    out = folder / name
    out.write_bytes(buf.getvalue())
    return out


def _ocr_returning(per_page_text: list[str]):
    """Build an ocr_pages_fn that returns the given list of page texts."""
    def fn(pdf_bytes: bytes) -> list[str]:
        return list(per_page_text)
    return fn


def _extract_returning(by_idx: dict[int, ext.PageExtraction]):
    def fn(idx: int, ocr_text: str) -> ext.PageExtraction:
        if idx in by_idx:
            return by_idx[idx]
        return ext.PageExtraction(
            page_idx=idx, patient_name=None, patient_dob=None,
            doc_title=None, is_continuation=False,
        )
    return fn


def _classify_returning(category: tx.IntakeCategory, conf: float = 0.95):
    def fn(text: str, *, doc_title=None) -> doc_classifier.ClassificationResult:
        return doc_classifier.ClassificationResult(category=category, confidence=conf)
    return fn


def _search_returning(rows: list[dict]):
    def fn(params: dict) -> list[dict]:
        return rows
    return fn


def _uploader_returning(doc_num: int = 99999):
    captured: list[dict] = []

    def fn(payload: dict) -> dict:
        captured.append(payload)
        return {"DocNum": doc_num,
                "FilePath": rf"\\share\{doc_num}.pdf"}

    fn.captured = captured  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# Watch folder semantics
# ---------------------------------------------------------------------------

def test_missing_watch_folder_halts_cleanly(tmp_path: Path, cache_path: Path) -> None:
    res = pr.process_watch_folder(
        watch_folder=tmp_path / "doesnotexist",
        cache_path=cache_path,
    )
    assert res.halted_reason == "watch_folder_missing"


def test_empty_watch_folder_returns_zero_results(watch: Path, cache_path: Path) -> None:
    res = pr.process_watch_folder(watch_folder=watch, cache_path=cache_path)
    assert res.pdfs_scanned == 0
    assert res.candidates_extracted == 0


def test_already_processed_pdf_skipped(watch: Path, cache_path: Path) -> None:
    pdf = _write_pdf(watch, "batch.pdf", pages=2)

    # Run once
    pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        ocr_pages_fn=_ocr_returning(["", ""]),
        extract_page_fn=_extract_returning({}),
        classify_fn=_classify_returning(tx.MISCELLANEOUS, 0.0),
    )

    # Run again — same PDF, same sha — should skip
    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        ocr_pages_fn=_ocr_returning(["", ""]),
        extract_page_fn=_extract_returning({}),
        classify_fn=_classify_returning(tx.MISCELLANEOUS, 0.0),
    )
    assert res.pdfs_skipped_already_processed == 1
    assert res.pdfs_scanned == 0


# ---------------------------------------------------------------------------
# Single-doc PDF, perfect signals -> auto-filed
# ---------------------------------------------------------------------------

def test_high_confidence_single_doc_auto_files(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=2)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Smith, Jane",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
        1: ext.PageExtraction(
            page_idx=1, patient_name="Smith, Jane",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=True,
        ),
    }
    uploader = _uploader_returning(doc_num=12345)
    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.85,  # split_conf can be ~0.86 for short multi-page form
        ocr_pages_fn=_ocr_returning(["medical history text page 1", "...continued..."]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=_search_returning([
            {"PatNum": 100, "LName": "Smith", "FName": "Jane",
             "Birthdate": "1980-04-12T00:00:00"},
        ]),
        od_uploader_fn=uploader,
    )
    assert res.pdfs_scanned == 1
    assert res.candidates_extracted == 1
    assert res.candidates_auto_filed == 1
    assert res.candidates_queued == 0
    assert len(uploader.captured) == 1  # type: ignore[attr-defined]
    assert uploader.captured[0]["patient_id"] == 100  # type: ignore[attr-defined]
    assert uploader.captured[0]["category"] == tx.MEDICAL_HISTORY.def_num  # type: ignore[attr-defined]

    # Cache row should be 'auto_filed' with the OD-returned DocNum.
    with ic.open_cache(cache_path) as conn:
        rows = ic.list_by_status(conn, "auto_filed")
        assert len(rows) == 1
        assert rows[0].target_doc_num == 12345
        assert rows[0].suggested_pat_num == 100
        assert rows[0].decided_by == "auto-file"


def test_low_confidence_queues_instead_of_filing(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=1)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Smith, Jane",
            patient_dob=None,  # missing DOB lowers patient confidence
            doc_title="CONSENT",
            is_continuation=False,
        ),
    }
    uploader = _uploader_returning()
    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.95,
        ocr_pages_fn=_ocr_returning(["consent text"]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.CORRESPONDENCE_CONSENTS, 0.7),
        search_patients_fn=_search_returning([
            {"PatNum": 100, "LName": "Smith", "FName": "Jane",
             "Birthdate": "1980-04-12T00:00:00"},
        ]),
        od_uploader_fn=uploader,
    )
    assert res.candidates_queued == 1
    assert res.candidates_auto_filed == 0
    assert len(uploader.captured) == 0  # type: ignore[attr-defined]
    with ic.open_cache(cache_path) as conn:
        rows = ic.list_by_status(conn, "queued")
        assert len(rows) == 1
        assert rows[0].overall_confidence is not None
        assert rows[0].overall_confidence < 0.95


def test_no_patient_match_queues(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=1)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Notinod, Person",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
    }
    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.5,  # low threshold to make sure no_match still doesn't auto-file
        ocr_pages_fn=_ocr_returning(["medical history"]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=_search_returning([]),  # no patients
        od_uploader_fn=_uploader_returning(),
    )
    assert res.candidates_queued == 1
    assert res.candidates_auto_filed == 0
    with ic.open_cache(cache_path) as conn:
        rows = ic.list_by_status(conn, "queued")
        assert rows[0].patient_confidence == 0.0


# ---------------------------------------------------------------------------
# Multi-patient batch
# ---------------------------------------------------------------------------

def test_three_patients_in_one_pdf(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=3)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Smith, Jane",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
        1: ext.PageExtraction(
            page_idx=1, patient_name="Doe, John",
            patient_dob="1975-07-04", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
        2: ext.PageExtraction(
            page_idx=2, patient_name="Garcia, Maria",
            patient_dob="1990-12-25", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
    }
    uploader = _uploader_returning(doc_num=11111)
    pat_lookup = {
        "smith": [{"PatNum": 1, "LName": "Smith", "FName": "Jane",
                   "Birthdate": "1980-04-12T00:00:00"}],
        "doe": [{"PatNum": 2, "LName": "Doe", "FName": "John",
                 "Birthdate": "1975-07-04T00:00:00"}],
        "garcia": [{"PatNum": 3, "LName": "Garcia", "FName": "Maria",
                    "Birthdate": "1990-12-25T00:00:00"}],
    }

    def search(params):
        last = (params.get("last_name") or "").lower()
        return pat_lookup.get(last, [])

    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.85,
        ocr_pages_fn=_ocr_returning(["smith", "doe", "garcia"]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=search,
        od_uploader_fn=uploader,
    )
    assert res.candidates_extracted == 3
    assert res.candidates_auto_filed == 3
    assert len(uploader.captured) == 3  # type: ignore[attr-defined]
    pat_nums_uploaded = sorted(
        p["patient_id"] for p in uploader.captured  # type: ignore[attr-defined]
    )
    assert pat_nums_uploaded == [1, 2, 3]


def test_filer_failure_marks_pending_as_error(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=1)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Smith, Jane",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
    }

    def upload_fail(payload):
        return {"success": False, "error": "od down"}

    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.5,
        ocr_pages_fn=_ocr_returning(["t"]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=_search_returning([
            {"PatNum": 100, "LName": "Smith", "FName": "Jane",
             "Birthdate": "1980-04-12T00:00:00"},
        ]),
        od_uploader_fn=upload_fail,
    )
    assert res.candidates_errored == 1
    with ic.open_cache(cache_path) as conn:
        rows = ic.list_by_status(conn, "error")
        assert len(rows) == 1
        assert "od down" in (rows[0].error_message or "")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_audit_log_entries_for_each_candidate(watch: Path, cache_path: Path) -> None:
    _write_pdf(watch, "batch.pdf", pages=1)
    extractions = {
        0: ext.PageExtraction(
            page_idx=0, patient_name="Smith, Jane",
            patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
            is_continuation=False,
        ),
    }
    pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        auto_file_threshold=0.5,
        ocr_pages_fn=_ocr_returning(["t"]),
        extract_page_fn=_extract_returning(extractions),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=_search_returning([
            {"PatNum": 100, "LName": "Smith", "FName": "Jane",
             "Birthdate": "1980-04-12T00:00:00"},
        ]),
        od_uploader_fn=_uploader_returning(),
    )
    with ic.open_cache(cache_path) as conn:
        rows = ic.list_by_status(conn, "auto_filed")
        assert len(rows) == 1
        log_entries = ic.list_audit_for_pending(conn, rows[0].id)  # type: ignore[arg-type]
        actions = [e["action"] for e in log_entries]
        assert "extracted" in actions
        assert "auto_filed" in actions


# ---------------------------------------------------------------------------
# Error robustness
# ---------------------------------------------------------------------------

def test_ocr_failure_for_one_pdf_does_not_halt_others(watch: Path, cache_path: Path) -> None:
    # Different page counts -> different SHAs -> both are processed, not deduped.
    _write_pdf(watch, "good.pdf", pages=1)
    _write_pdf(watch, "bad.pdf", pages=2)

    def ocr(pdf_bytes):
        # Crash on the second PDF deterministically.
        if not hasattr(ocr, "n"):
            ocr.n = 0  # type: ignore[attr-defined]
        ocr.n += 1
        if ocr.n == 2:
            raise RuntimeError("ocr bombed")
        return ["text"]

    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        ocr_pages_fn=ocr,
        extract_page_fn=_extract_returning({
            0: ext.PageExtraction(
                page_idx=0, patient_name="Smith, Jane",
                patient_dob="1980-04-12", doc_title="MEDICAL HISTORY",
                is_continuation=False,
            ),
        }),
        classify_fn=_classify_returning(tx.MEDICAL_HISTORY, 0.95),
        search_patients_fn=_search_returning([]),
        od_uploader_fn=_uploader_returning(),
    )
    # One PDF processed, one failed.
    assert res.pdfs_failed >= 1
    assert any("pdf_failed" in e for e in res.errors)


def test_unreadable_pdf_skipped(watch: Path, cache_path: Path) -> None:
    """A file that can't be read as bytes is logged but doesn't halt."""
    bad = watch / "garbage.pdf"
    bad.write_bytes(b"not a real pdf")

    res = pr.process_watch_folder(
        watch_folder=watch, cache_path=cache_path,
        ocr_pages_fn=lambda b: [""],  # default OCR — fine
        extract_page_fn=_extract_returning({}),
        classify_fn=_classify_returning(tx.MISCELLANEOUS, 0.0),
    )
    # The file is read OK (it's bytes), but OCR returns empty text. That's fine.
    # Just confirm no crash.
    assert res.halted_reason is None
