"""Tests for the four new MCP tool wrappers in mcp_tools.OpenDentalMCPTools.

We instantiate the real class and stub:
  - tools._query_database  -> FakeTools._query_database
  - preprocessing.document_text_cache.DEFAULT_CACHE_PATH -> tmp file
  - the OCR function via preprocessing.document_text_index.fetch_or_ocr / backfill paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from preprocessing import document_text_cache as cache
from preprocessing import document_text_index as idx
from preprocessing import ocr_helper

import mcp_tools

from tests.conftest import FakeTools


@pytest.fixture
def tools_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_tools: FakeTools,
) -> mcp_tools.OpenDentalMCPTools:
    cache_file = tmp_path / "doc_text.db"
    monkeypatch.setattr(cache, "DEFAULT_CACHE_PATH", cache_file)
    inst = mcp_tools.OpenDentalMCPTools()
    # Route DB queries through FakeTools.
    monkeypatch.setattr(inst, "_query_database", fake_tools._query_database)
    # Stash the fake on the instance for tests that need to push rows.
    inst._fake_tools = fake_tools  # type: ignore[attr-defined]
    return inst


def _stub_ocr_fn(text: str = "hello"):
    def fn(file_bytes: bytes, *, media_type: str, **kw):
        return ocr_helper.OcrResult(
            text=text, model="haiku", input_tokens=10, output_tokens=5,
            cost_usd=0.0001, media_type=media_type, is_unreadable=False,
        )
    return fn


def _doc_row(doc_num: int, pat_num: int = 100, lname: str = "Young", fname: str = "Ben") -> dict:
    return {
        "DocNum": doc_num, "PatNum": pat_num, "FileName": "consent.jpg",
        "DateCreated": "2026-04-01", "DocCategory": 5,
        "LName": lname, "FName": fname,
    }


# ---------------------------------------------------------------------------
# _get_document_text
# ---------------------------------------------------------------------------

def test_get_document_text_invalid_doc_num(tools_instance) -> None:
    res = tools_instance._get_document_text("not-a-number")
    assert res["success"] is False
    assert "integer" in res["error"]


def test_get_document_text_cache_hit(
    tools_instance, tmp_path: Path,
) -> None:
    # Pre-populate cache.
    with cache.open_cache() as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=42, PatNum=10, FileName="x.jpg", Text="hello",
            OcrAt="2026-04-01", Status="ok",
        ))
    res = tools_instance._get_document_text(42)
    assert res["success"] is True
    assert res["source"] == "cache"
    assert res["text"] == "hello"
    assert res["doc_num"] == 42


def test_get_document_text_cache_miss_triggers_ocr(
    tools_instance, monkeypatch: pytest.MonkeyPatch, share_root: Path,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [_doc_row(99)])
    folder = share_root / "Y" / "YoungBen100"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "consent.jpg").write_bytes(b"x")
    # Make resolve_doc_path use our temp share, and ocr_one_document use stub.
    monkeypatch.setenv("OD_DOC_ROOT", str(share_root))
    real_ocr_one = idx.ocr_one_document

    def stub_ocr_one(doc, **kw):
        kw["ocr_fn"] = _stub_ocr_fn(text="OCR'd")
        return real_ocr_one(doc, **kw)

    monkeypatch.setattr(idx, "ocr_one_document", stub_ocr_one)
    res = tools_instance._get_document_text(99)
    assert res["success"] is True
    assert res["source"] == "fresh"
    assert res["text"] == "OCR'd"


def test_get_document_text_missing_in_od(tools_instance) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [])
    res = tools_instance._get_document_text(12345)
    assert res["success"] is False
    assert "not found" in res["error"]


def test_get_document_text_handles_internal_exception(
    tools_instance, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(idx, "fetch_or_ocr", boom)
    res = tools_instance._get_document_text(1)
    assert res["success"] is False
    assert "kaboom" in res["error"]


# ---------------------------------------------------------------------------
# _get_patient_document_texts
# ---------------------------------------------------------------------------

def test_get_patient_document_texts_invalid_pat(tools_instance) -> None:
    res = tools_instance._get_patient_document_texts(pat_num="oops")
    assert res["success"] is False


def test_get_patient_document_texts_reports_gap(
    tools_instance,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    # OD says 5 docs exist for this patient, cache has 2.
    fake.push_rows("SELECT", [{"n": 5}])
    with cache.open_cache() as conn:
        for n in (1, 2):
            cache.put_text(conn, cache.DocTextRow(
                DocNum=n, PatNum=10, Text=f"doc {n}",
                OcrAt="2026-04-01", Status="ok",
            ))
    res = tools_instance._get_patient_document_texts(10)
    assert res["success"] is True
    assert res["total_in_od"] == 5
    assert res["cached"] == 2
    assert "not yet cached" in (res.get("note") or "")


def test_get_patient_document_texts_no_gap(
    tools_instance,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [{"n": 1}])
    with cache.open_cache() as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=1, PatNum=10, Text="doc",
            OcrAt="2026-04-01", Status="ok",
        ))
    res = tools_instance._get_patient_document_texts(10)
    assert res["total_in_od"] == 1
    assert res["cached"] == 1
    assert res.get("note") is None


def test_get_patient_document_texts_category_filter(
    tools_instance,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [{"n": 2}])
    with cache.open_cache() as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=1, PatNum=10, DocCategory=5, Text="A",
            OcrAt="2026-04-01", Status="ok",
        ))
        cache.put_text(conn, cache.DocTextRow(
            DocNum=2, PatNum=10, DocCategory=6, Text="B",
            OcrAt="2026-04-01", Status="ok",
        ))
    res = tools_instance._get_patient_document_texts(10, doc_category=5)
    assert res["cached"] == 1
    assert {d["doc_num"] for d in res["documents"]} == {1}


# ---------------------------------------------------------------------------
# _search_document_text
# ---------------------------------------------------------------------------

def test_search_document_text_empty_query(tools_instance) -> None:
    res = tools_instance._search_document_text(query="   ")
    assert res["success"] is False
    assert "non-empty" in res["error"]


def test_search_document_text_returns_matches(
    tools_instance,
) -> None:
    with cache.open_cache() as conn:
        cache.put_text(conn, cache.DocTextRow(
            DocNum=1, PatNum=10, Text="patient reports allergy to penicillin",
            OcrAt="2026-04-01", Status="ok", FileName="intake.jpg",
        ))
        cache.put_text(conn, cache.DocTextRow(
            DocNum=2, PatNum=11, Text="dental cleaning consent",
            OcrAt="2026-04-01", Status="ok", FileName="consent.jpg",
        ))
    res = tools_instance._search_document_text(query="allergy")
    assert res["success"] is True
    assert res["count"] == 1
    assert res["matches"][0]["doc_num"] == 1
    assert "allergy" in res["matches"][0]["snippet"].lower()


def test_search_document_text_handles_exception(
    tools_instance, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cache, "search", boom)
    res = tools_instance._search_document_text(query="anything")
    assert res["success"] is False
    assert "kaboom" in res["error"]


# ---------------------------------------------------------------------------
# _rebuild_document_text_index
# ---------------------------------------------------------------------------

def test_rebuild_dry_run_makes_no_ocr_calls(
    tools_instance, monkeypatch: pytest.MonkeyPatch, share_root: Path,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [_doc_row(1), _doc_row(2)])
    fake.push_rows("SELECT", [])
    calls = []
    monkeypatch.setattr(idx, "ocr_one_document",
                        lambda *a, **k: calls.append(1) or cache.DocTextRow(DocNum=0, PatNum=0))
    res = tools_instance._rebuild_document_text_index(max_docs=10, dry_run=True)
    assert res["success"] is True
    assert res["ocrd"] == 0
    assert res["scanned"] == 2
    assert calls == []


def test_rebuild_runs_ocr_when_not_dry(
    tools_instance, monkeypatch: pytest.MonkeyPatch, share_root: Path,
) -> None:
    fake = tools_instance._fake_tools  # type: ignore[attr-defined]
    fake.push_rows("SELECT", [_doc_row(1), _doc_row(2)])
    fake.push_rows("SELECT", [])
    monkeypatch.setenv("OD_DOC_ROOT", str(share_root))
    folder = share_root / "Y" / "YoungBen100"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "consent.jpg").write_bytes(b"x")

    real_one = idx.ocr_one_document

    def stub_one(doc, **kw):
        kw["ocr_fn"] = _stub_ocr_fn(text="ok")
        return real_one(doc, **kw)

    monkeypatch.setattr(idx, "ocr_one_document", stub_one)
    res = tools_instance._rebuild_document_text_index(max_docs=10, max_spend_usd=1.0)
    assert res["success"] is True
    assert res["ocrd"] == 2


def test_rebuild_handles_exception(
    tools_instance, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(idx, "backfill", boom)
    res = tools_instance._rebuild_document_text_index()
    assert res["success"] is False
    assert "kaboom" in res["error"]


# ---------------------------------------------------------------------------
# Tool registration sanity checks
# ---------------------------------------------------------------------------

def test_new_tools_appear_in_list_tools(tools_instance) -> None:
    names = {t["name"] for t in tools_instance.list_tools()}
    for n in (
        "get_document_text",
        "get_patient_document_texts",
        "search_document_text",
        "rebuild_document_text_index",
    ):
        assert n in names


def test_call_tool_dispatches_new_tools(
    tools_instance, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the implementations so we can verify the dispatch wiring without
    # exercising the full code path.
    seen: list[str] = []
    def stub(*a, **k):
        seen.append("called")
        return {"success": True}
    monkeypatch.setattr(tools_instance, "_get_document_text", stub)
    monkeypatch.setattr(tools_instance, "_get_patient_document_texts", stub)
    monkeypatch.setattr(tools_instance, "_search_document_text", stub)
    monkeypatch.setattr(tools_instance, "_rebuild_document_text_index", stub)

    tools_instance.call_tool("get_document_text", {"doc_num": 1})
    tools_instance.call_tool("get_patient_document_texts", {"pat_num": 1})
    tools_instance.call_tool("search_document_text", {"query": "x"})
    tools_instance.call_tool("rebuild_document_text_index", {})
    assert len(seen) == 4


def test_list_resources_includes_document_text_cache(tools_instance) -> None:
    res = tools_instance._list_resources()
    names = {r["name"] for r in res["resources"]}
    assert "document_text_cache" in names
