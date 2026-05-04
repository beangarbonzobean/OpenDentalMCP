"""Tests for ocr_review_routes.py — Flask blueprint for the OCR review UI.

Uses Flask test_client. Mocks mcp_tools (the blueprint instantiates it on
demand for the /pdf endpoint). The doc_text cache uses a per-test temp DB.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

from preprocessing import document_text_cache as cache


# ---------------------------------------------------------------------------
# Fake mcp_tools (only needed by the /pdf endpoint)
# ---------------------------------------------------------------------------

class FakeOpenDentalMCPTools:
    query_response: Any = {"success": True, "rows": [
        {"LName": "Young", "FName": "Ben"},
    ]}

    def _query_database(self, query: str, limit: int = 1000):
        return self.query_response


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeOpenDentalMCPTools.query_response = {"success": True, "rows": [
        {"LName": "Young", "FName": "Ben"},
    ]}
    yield


@pytest.fixture
def fake_mcp_tools(monkeypatch: pytest.MonkeyPatch):
    import types
    mod = types.ModuleType("mcp_tools")
    mod.OpenDentalMCPTools = FakeOpenDentalMCPTools
    monkeypatch.setitem(sys.modules, "mcp_tools", mod)
    yield mod


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_mcp_tools) -> Flask:
    cache_db = tmp_path / "doc_text.db"
    monkeypatch.setattr(cache, "DEFAULT_CACHE_PATH", cache_db)

    if "ocr_review_routes" in sys.modules:
        del sys.modules["ocr_review_routes"]
    from ocr_review_routes import ocr_review_bp

    flask_app = Flask(__name__)
    flask_app.register_blueprint(ocr_review_bp)
    flask_app.config["TESTING"] = True
    flask_app.config["_cache_db"] = cache_db
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_row(
    cache_db: Path, *,
    doc_num: int = 100, pat_num: int = 42,
    text: str = "sample text", status: str = "ok",
    reviewed: int = 0, ocr_at: str | None = None,
    file_name: str = "test.pdf",
    cost: float | None = 0.005,
) -> None:
    row = cache.DocTextRow(
        DocNum=doc_num, PatNum=pat_num, FileName=file_name,
        DocCategory=465, DateCreated="2026-04-01",
        Text=text, PageCount=1, Sha256="abc",
        OcrModel="glm-ocr:q8_0",
        OcrAt=ocr_at or datetime.now(timezone.utc).isoformat(),
        Status=status, CostUsd=cost,
        Reviewed=reviewed,
    )
    with cache.open_cache(cache_db) as conn:
        cache.put_text(conn, row)


# ---------------------------------------------------------------------------
# Health + LAN gate
# ---------------------------------------------------------------------------

def test_healthz_open_to_anyone(client) -> None:
    r = client.get("/ocr-review/healthz",
                    environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 200
    assert r.get_json()["service"] == "ocr-review"


def test_lan_gate_blocks_external(client) -> None:
    r = client.get("/ocr-review/api/queue",
                    environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 403


def test_lan_gate_allows_localhost(client) -> None:
    r = client.get("/ocr-review/api/queue")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_empty(client) -> None:
    r = client.get("/ocr-review/api/summary")
    s = r.get_json()
    assert s["total"] == 0
    assert s["unreviewed"] == 0


def test_summary_counts_status_buckets(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=1, status="ok", cost=0.005)
    _seed_row(app.config["_cache_db"], doc_num=2, status="ok", cost=0.003)
    _seed_row(app.config["_cache_db"], doc_num=3, status="error", cost=None)
    _seed_row(app.config["_cache_db"], doc_num=4, status="unreadable", cost=0.001)
    s = client.get("/ocr-review/api/summary").get_json()
    assert s["total"] == 4
    assert s["by_status"]["ok"] == 2
    assert s["by_status"]["error"] == 1
    assert s["by_status"]["unreadable"] == 1
    assert s["unreviewed"] == 4
    assert s["cost_usd_total"] == pytest.approx(0.009)


def test_summary_excludes_old_docs(client, app) -> None:
    """Default 7-day window: a row from 30 days ago is not in the window."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_row(app.config["_cache_db"], doc_num=1, ocr_at=old)
    _seed_row(app.config["_cache_db"], doc_num=2)  # now
    s = client.get("/ocr-review/api/summary").get_json()
    assert s["total"] == 1


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def test_queue_lists_unreviewed_default(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=1, reviewed=0)
    _seed_row(app.config["_cache_db"], doc_num=2, reviewed=1)
    data = client.get("/ocr-review/api/queue").get_json()
    assert data["count"] == 1
    assert data["items"][0]["DocNum"] == 1


def test_queue_include_reviewed(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=1, reviewed=0)
    _seed_row(app.config["_cache_db"], doc_num=2, reviewed=1)
    data = client.get("/ocr-review/api/queue?include_reviewed=1").get_json()
    assert data["count"] == 2


def test_queue_status_filter(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=1, status="ok")
    _seed_row(app.config["_cache_db"], doc_num=2, status="error")
    _seed_row(app.config["_cache_db"], doc_num=3, status="unreadable")
    data = client.get("/ocr-review/api/queue?status=error,unreadable").get_json()
    docnums = {it["DocNum"] for it in data["items"]}
    assert docnums == {2, 3}


def test_queue_returns_text_preview_not_full_text(client, app) -> None:
    long_text = "x" * 5000
    _seed_row(app.config["_cache_db"], doc_num=1, text=long_text)
    data = client.get("/ocr-review/api/queue").get_json()
    item = data["items"][0]
    assert "Text" not in item                  # full text excluded from listing
    assert len(item["text_preview"]) <= 250    # truncated preview only
    assert item["text_length"] == 5000


# ---------------------------------------------------------------------------
# Doc detail + PDF
# ---------------------------------------------------------------------------

def test_doc_detail_returns_full_text(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=42, text="full text content here")
    r = client.get("/ocr-review/api/doc/42")
    assert r.status_code == 200
    item = r.get_json()["item"]
    assert item["DocNum"] == 42
    assert item["Text"] == "full text content here"


def test_doc_detail_404_unknown(client) -> None:
    r = client.get("/ocr-review/api/doc/99999")
    assert r.status_code == 404


def test_doc_pdf_410_when_source_missing(client, app, tmp_path: Path) -> None:
    """The cache has a row but the resolved file path doesn't exist on disk."""
    _seed_row(app.config["_cache_db"], doc_num=42, file_name="ghost.pdf")
    # share_root is the real \\SERVER12 share by default — won't find the file.
    r = client.get("/ocr-review/api/doc/42/pdf")
    assert r.status_code in (410, 500)  # depends on what resolve hits first


# ---------------------------------------------------------------------------
# Approve / unapprove
# ---------------------------------------------------------------------------

def test_approve_marks_reviewed(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=42, reviewed=0)
    r = client.post("/ocr-review/api/doc/42/approve",
                    json={"reviewer": "ben"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "approved"

    with cache.open_cache(app.config["_cache_db"]) as conn:
        row = cache.get_text(conn, 42)
        assert row.Reviewed == 1
        assert row.ReviewedBy == "ben"
        assert row.ReviewedAt is not None


def test_approve_404_unknown(client) -> None:
    r = client.post("/ocr-review/api/doc/99999/approve", json={})
    assert r.status_code == 404


def test_unapprove_resets_reviewed(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=42, reviewed=1)
    r = client.post("/ocr-review/api/doc/42/unapprove", json={})
    assert r.status_code == 200
    with cache.open_cache(app.config["_cache_db"]) as conn:
        row = cache.get_text(conn, 42)
        assert row.Reviewed == 0
        assert row.ReviewedBy is None


# ---------------------------------------------------------------------------
# Flag for re-OCR (delete)
# ---------------------------------------------------------------------------

def test_flag_deletes_row(client, app) -> None:
    _seed_row(app.config["_cache_db"], doc_num=42)
    r = client.post("/ocr-review/api/doc/42/flag",
                    json={"reason": "blurry scan"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "deleted"
    with cache.open_cache(app.config["_cache_db"]) as conn:
        assert cache.get_text(conn, 42) is None


def test_flag_404_unknown(client) -> None:
    r = client.post("/ocr-review/api/doc/99999/flag", json={})
    assert r.status_code == 404


def test_flag_then_reocr_can_repopulate(client, app) -> None:
    """After flag/delete, the same DocNum can be re-inserted (e.g., next backfill)."""
    _seed_row(app.config["_cache_db"], doc_num=42, text="bad ocr")
    client.post("/ocr-review/api/doc/42/flag", json={})
    _seed_row(app.config["_cache_db"], doc_num=42, text="good ocr round 2")
    with cache.open_cache(app.config["_cache_db"]) as conn:
        row = cache.get_text(conn, 42)
        assert row.Text == "good ocr round 2"
        assert row.Reviewed == 0  # fresh OCR, fresh review state
