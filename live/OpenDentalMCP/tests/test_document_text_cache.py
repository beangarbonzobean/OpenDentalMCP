"""Tests for preprocessing.document_text_cache."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from preprocessing.document_text_cache import (
    DocTextRow,
    cached_doc_nums,
    find_ok_by_sha256,
    get_text,
    get_texts_for_patient,
    init_cache,
    open_cache,
    prune_orphans,
    put_text,
    search,
)


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "doc_text.db"


def _row(
    doc_num: int = 1,
    pat_num: int = 100,
    text: str = "",
    sha: str | None = "abc",
    file_name: str | None = "consent.jpg",
    category: int | None = 5,
    status: str = "ok",
) -> DocTextRow:
    return DocTextRow(
        DocNum=doc_num,
        PatNum=pat_num,
        FileName=file_name,
        DocCategory=category,
        DateCreated="2026-04-01",
        Text=text,
        PageCount=1,
        Sha256=sha,
        OcrModel="claude-haiku-4-5-20251001",
        OcrAt=datetime.now(timezone.utc).isoformat(),
        Status=status,
        CostUsd=0.001,
    )


def test_init_cache_creates_file_and_schema(cache_path: Path) -> None:
    p = init_cache(cache_path)
    assert p == cache_path
    assert p.exists()
    with open_cache(cache_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
        assert "doc_text" in names
        assert "doc_text_fts" in names
        assert "idx_doc_text_pat" in names


def test_wal_mode_after_open(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_put_then_get_roundtrip(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=42, text="Hello world", sha="hash1"))
        r = get_text(conn, 42)
        assert r is not None
        assert r.DocNum == 42
        assert r.Text == "Hello world"
        assert r.Sha256 == "hash1"
        assert r.Status == "ok"


def test_get_text_missing_returns_none(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        assert get_text(conn, 999) is None


def test_put_text_upsert_replaces_text(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, text="Old", sha="h1"))
        put_text(conn, _row(doc_num=1, text="New", sha="h2"))
        r = get_text(conn, 1)
        assert r is not None
        assert r.Text == "New"
        assert r.Sha256 == "h2"
        # Only one row total.
        count = conn.execute("SELECT COUNT(*) FROM doc_text").fetchone()[0]
        assert count == 1


def test_invalid_status_rejected(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        with pytest.raises(ValueError):
            put_text(conn, _row(status="bogus"))


def test_get_texts_for_patient(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, pat_num=10, text="A", category=5))
        put_text(conn, _row(doc_num=2, pat_num=10, text="B", category=6))
        put_text(conn, _row(doc_num=3, pat_num=11, text="C", category=5))
        all_for_10 = get_texts_for_patient(conn, 10)
        assert {r.DocNum for r in all_for_10} == {1, 2}
        cat5_for_10 = get_texts_for_patient(conn, 10, doc_category=5)
        assert {r.DocNum for r in cat5_for_10} == {1}


def test_cached_doc_nums(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        for n in (1, 5, 9):
            put_text(conn, _row(doc_num=n, text="x"))
        assert cached_doc_nums(conn) == {1, 5, 9}


def test_terminal_doc_nums_excludes_errors(cache_path: Path) -> None:
    from preprocessing.document_text_cache import terminal_doc_nums
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, status="ok", text="hello"))
        put_text(conn, _row(doc_num=2, status="unreadable"))
        put_text(conn, _row(doc_num=3, status="unsupported"))
        put_text(conn, _row(doc_num=4, status="error"))
        # Terminal = will not change on retry.
        assert terminal_doc_nums(conn) == {1, 2, 3}
        # cached_doc_nums returns everything.
        assert cached_doc_nums(conn) == {1, 2, 3, 4}


def test_search_keyword_match_and_pat_filter(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, pat_num=10, text="patient reports allergy to penicillin"))
        put_text(conn, _row(doc_num=2, pat_num=11, text="dental cleaning consent form"))
        put_text(conn, _row(doc_num=3, pat_num=10, text="no allergies disclosed"))

        hits_all = search(conn, "allergy")
        # FTS5 with porter stemming matches both "allergy" and "allergies".
        assert {h.DocNum for h in hits_all} == {1, 3}

        hits_p11 = search(conn, "consent", pat_num=11)
        assert {h.DocNum for h in hits_p11} == {2}

        hits_p10_consent = search(conn, "consent", pat_num=10)
        assert hits_p10_consent == []


def test_search_doc_category_filter(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, pat_num=10, text="allergy info", category=5))
        put_text(conn, _row(doc_num=2, pat_num=10, text="allergy info", category=6))
        hits = search(conn, "allergy", doc_category=5)
        assert {h.DocNum for h in hits} == {1}


def test_search_returns_snippet(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, pat_num=10, text="The patient is allergic to penicillin and aspirin."))
        hits = search(conn, "penicillin")
        assert len(hits) == 1
        assert "penicillin" in hits[0].Snippet.lower()


def test_search_empty_query_returns_empty(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(text="hello"))
        assert search(conn, "") == []
        assert search(conn, "   ") == []


def test_search_quotes_dont_break_query(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(text="amoxicillin clavulanate"))
        hits = search(conn, 'amox"icillin')
        # Sanitizer strips embedded quotes; resulting token still matches.
        assert len(hits) == 1


def test_prune_orphans(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        for n in (1, 2, 3, 4):
            put_text(conn, _row(doc_num=n, text=f"doc {n}"))
        deleted = prune_orphans(conn, [1, 3])
        assert deleted == 2
        assert cached_doc_nums(conn) == {1, 3}


def test_prune_orphans_empty_input_is_noop(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        for n in (1, 2):
            put_text(conn, _row(doc_num=n, text="x"))
        deleted = prune_orphans(conn, [])
        assert deleted == 0
        assert cached_doc_nums(conn) == {1, 2}


def test_find_ok_by_sha256_basic(cache_path: Path) -> None:
    """Basic dedup lookup: existing ok row with matching SHA returns it."""
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, sha="aaaa", text="hello world from doc 1"))
        hit = find_ok_by_sha256(conn, "aaaa")
        assert hit is not None
        assert hit.DocNum == 1
        assert hit.Text == "hello world from doc 1"


def test_find_ok_by_sha256_missing_returns_none(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, sha="aaaa", text="x" * 50))
        assert find_ok_by_sha256(conn, "bbbb") is None


def test_find_ok_by_sha256_skips_non_ok_rows(cache_path: Path) -> None:
    """unreadable / error / unsupported rows must NOT be treated as dedup
    sources — would propagate bad results."""
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, sha="dup", status="unreadable", text=""))
        put_text(conn, _row(doc_num=2, sha="dup", status="error", text=""))
        put_text(conn, _row(doc_num=3, sha="dup", status="unsupported", text=""))
        assert find_ok_by_sha256(conn, "dup") is None
        # Now add an ok row — it should be returned
        put_text(conn, _row(doc_num=4, sha="dup", text="real content here longer"))
        hit = find_ok_by_sha256(conn, "dup")
        assert hit is not None
        assert hit.DocNum == 4


def test_find_ok_by_sha256_returns_smallest_doc_num(cache_path: Path) -> None:
    """When multiple ok rows share a SHA, deterministic order: oldest DocNum."""
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=99, sha="x", text="row 99 content for sha"))
        put_text(conn, _row(doc_num=10, sha="x", text="row 10 content for sha"))
        put_text(conn, _row(doc_num=50, sha="x", text="row 50 content for sha"))
        hit = find_ok_by_sha256(conn, "x")
        assert hit is not None
        assert hit.DocNum == 10


def test_find_ok_by_sha256_empty_or_none_sha(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, sha="aaa", text="x" * 50))
        assert find_ok_by_sha256(conn, "") is None


def test_find_ok_by_sha256_min_chars_filter(cache_path: Path) -> None:
    """min_text_chars guards against propagating very short ok rows."""
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, sha="aaa", text="ok"))  # 2 chars
        put_text(conn, _row(doc_num=2, sha="bbb", text="this is a longer ok row"))
        # Default 1 returns the short one
        assert find_ok_by_sha256(conn, "aaa") is not None
        # Higher floor skips it
        assert find_ok_by_sha256(conn, "aaa", min_text_chars=10) is None
        assert find_ok_by_sha256(conn, "bbb", min_text_chars=10) is not None


def test_concurrent_writes_no_corruption(cache_path: Path) -> None:
    init_cache(cache_path)

    def writer(start: int) -> None:
        # Each thread uses its own connection — sqlite3 connections are not thread-safe.
        with open_cache(cache_path) as conn:
            for i in range(10):
                n = start + i
                put_text(conn, _row(doc_num=n, text=f"doc {n}"))

    threads = [threading.Thread(target=writer, args=(s,)) for s in (100, 200, 300, 400)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open_cache(cache_path) as conn:
        nums = cached_doc_nums(conn)
        assert len(nums) == 40


def test_fts_index_updated_on_text_change(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, text="apple banana"))
        assert {h.DocNum for h in search(conn, "banana")} == {1}
        # Change text via upsert
        put_text(conn, _row(doc_num=1, text="cherry date"))
        # Old token should no longer hit
        assert search(conn, "banana") == []
        # New token should hit
        assert {h.DocNum for h in search(conn, "cherry")} == {1}


def test_fts_index_updated_on_delete(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(doc_num=1, text="orange"))
        assert {h.DocNum for h in search(conn, "orange")} == {1}
        prune_orphans(conn, [99])
        assert search(conn, "orange") == []


def test_integrity_check_passes(cache_path: Path) -> None:
    with open_cache(cache_path) as conn:
        put_text(conn, _row(text="a b c"))
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert result == "ok"
