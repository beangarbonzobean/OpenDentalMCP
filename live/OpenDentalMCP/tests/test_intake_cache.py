"""Tests for preprocessing.intake.cache."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from preprocessing.intake import cache as ic


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "intake.db"


def _row(
    *,
    source_pdf: str = "C:/scans/batch1.pdf",
    sha: str = "abc123",
    pages: list[int] | None = None,
    name: str | None = "Smith, Jane",
    dob: str | None = "1980-04-12",
    pat: int | None = 100,
    label: str | None = "patient_info",
    def_num: int | None = 138,
    pat_conf: float | None = 0.95,
    cat_conf: float | None = 0.9,
    status: str = "pending",
) -> ic.IntakePending:
    return ic.IntakePending(
        source_pdf=source_pdf,
        source_pdf_sha256=sha,
        page_indices=pages or [0, 1],
        extracted_name=name,
        extracted_dob=dob,
        extracted_text_len=1500,
        suggested_pat_num=pat,
        suggested_pat_label=f"{name} ({pat})" if pat else None,
        suggested_category=label,
        suggested_def_num=def_num,
        patient_confidence=pat_conf,
        category_confidence=cat_conf,
        split_confidence=0.95,
        overall_confidence=0.85,
        status=status,
    )


def test_init_creates_schema(db_path: Path) -> None:
    p = ic.init_cache(db_path)
    assert p == db_path
    assert p.exists()
    with ic.open_cache(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "intake_pending" in names
        assert "intake_audit" in names
        assert "intake_processed_pdfs" in names


def test_wal_mode(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_insert_and_get_pending(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row())
        assert pid > 0
        got = ic.get_pending(conn, pid)
        assert got is not None
        assert got.extracted_name == "Smith, Jane"
        assert got.suggested_pat_num == 100
        assert got.page_indices == [0, 1]
        assert got.status == "pending"


def test_get_pending_missing_returns_none(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        assert ic.get_pending(conn, 9999) is None


def test_invalid_status_rejected(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        with pytest.raises(ValueError):
            ic.insert_pending(conn, _row(status="bogus"))


def test_list_by_status(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        ic.insert_pending(conn, _row(status="pending"))
        ic.insert_pending(conn, _row(status="queued"))
        ic.insert_pending(conn, _row(status="queued"))
        ic.insert_pending(conn, _row(status="filed"))
        assert len(ic.list_by_status(conn, "queued")) == 2
        assert len(ic.list_by_status(conn, "pending")) == 1
        assert len(ic.list_by_status(conn, "rejected")) == 0


def test_update_status(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row(status="pending"))
        ic.update_pending_status(
            conn, pid,
            status="filed",
            target_doc_num=12345,
            target_file_path=r"\\share\Y\YoungBen42\file.pdf",
            decided_by="staff:Ben",
        )
        got = ic.get_pending(conn, pid)
        assert got is not None
        assert got.status == "filed"
        assert got.target_doc_num == 12345
        assert got.target_file_path is not None
        assert got.decided_by == "staff:Ben"
        assert got.decided_at is not None


def test_update_status_can_override_patient_and_category(db_path: Path) -> None:
    """Staff override path: staff fixed both fields before confirming."""
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row(status="queued", pat=100, def_num=138))
        ic.update_pending_status(
            conn, pid,
            status="overridden",
            suggested_pat_num=200,
            suggested_def_num=455,
            decided_by="staff:Ben",
        )
        got = ic.get_pending(conn, pid)
        assert got is not None
        assert got.suggested_pat_num == 200
        assert got.suggested_def_num == 455
        assert got.status == "overridden"


def test_update_status_invalid_rejected(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row())
        with pytest.raises(ValueError):
            ic.update_pending_status(conn, pid, status="bogus")


def test_audit_round_trip(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row())
        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pid, action="extracted", actor="system",
            details={"split_confidence": 0.95, "ocr_chars": 1500},
        ))
        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pid, action="filed", actor="staff:Ben",
            details={"doc_num": 12345, "def_num": 138, "path": r"\\share\..."},
        ))
        log = ic.list_audit_for_pending(conn, pid)
        assert len(log) == 2
        assert log[0]["action"] == "extracted"
        assert log[1]["action"] == "filed"
        assert log[1]["details"]["doc_num"] == 12345


def test_processed_pdfs_idempotent_check(db_path: Path) -> None:
    with ic.open_cache(db_path) as conn:
        assert not ic.is_pdf_processed(conn, "abc")
        ic.mark_pdf_processed(conn, "abc", "C:/scans/x.pdf", page_count=10, candidates=4)
        assert ic.is_pdf_processed(conn, "abc")
        # Same sha treated as already done.
        ic.mark_pdf_processed(conn, "abc", "C:/scans/x.pdf", page_count=10, candidates=5)
        # No duplicate row.
        rows = list(conn.execute("SELECT COUNT(*) FROM intake_processed_pdfs"))
        assert rows[0][0] == 1


def test_concurrent_inserts(db_path: Path) -> None:
    ic.init_cache(db_path)

    def writer(start: int) -> None:
        with ic.open_cache(db_path) as conn:
            for i in range(10):
                ic.insert_pending(conn, _row(sha=f"sha-{start}-{i}"))

    threads = [threading.Thread(target=writer, args=(s,)) for s in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with ic.open_cache(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM intake_pending").fetchone()[0]
        assert n == 40


def test_page_indices_serialized_as_json(db_path: Path) -> None:
    """Sanity: list survives the SQLite round-trip via JSON."""
    with ic.open_cache(db_path) as conn:
        pid = ic.insert_pending(conn, _row(pages=[5, 6, 7, 8, 9]))
        got = ic.get_pending(conn, pid)
        assert got is not None
        assert got.page_indices == [5, 6, 7, 8, 9]
