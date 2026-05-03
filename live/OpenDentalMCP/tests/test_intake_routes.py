"""Tests for intake_routes.py Flask blueprint.

Uses Flask's test_client. The blueprint is registered on a fresh Flask app per
test so we can swap out env vars / mocked dependencies cleanly. The mcp_tools
dependency is replaced with a fake that exposes the same _search_patients and
_upload_document methods.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

# Make live/OpenDentalMCP/ importable as if we're the running service.
_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

from preprocessing.intake import cache as ic
from preprocessing.intake import taxonomy as tx


# ---------------------------------------------------------------------------
# Fake mcp_tools that the blueprint will pick up via _get_tools_instance
# ---------------------------------------------------------------------------

class FakeOpenDentalMCPTools:
    """Stand-in for the real OpenDentalMCPTools that the blueprint imports
    via `import mcp_tools`. The blueprint instantiates this class on every
    request that needs OD access."""

    # Class-level state so tests can read what the routes did.
    upload_calls: list[dict] = []
    upload_response: Any = {"DocNum": 99999,
                             "FileName": "test.pdf",
                             "FilePath": r"\\share\test.pdf"}
    search_response: Any = []
    search_calls: list[dict] = []

    def _search_patients(self, params):
        self.search_calls.append(dict(params))
        return self.search_response

    def _upload_document(self, payload):
        self.upload_calls.append(dict(payload))
        return self.upload_response


@pytest.fixture(autouse=True)
def _reset_fake_tools_state():
    FakeOpenDentalMCPTools.upload_calls.clear()
    FakeOpenDentalMCPTools.search_calls.clear()
    FakeOpenDentalMCPTools.upload_response = {
        "DocNum": 99999, "FileName": "test.pdf", "FilePath": r"\\share\test.pdf",
    }
    FakeOpenDentalMCPTools.search_response = []
    yield


@pytest.fixture
def fake_mcp_tools(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake `mcp_tools` module into sys.modules so the blueprint
    picks it up when it does `import mcp_tools`."""
    import types
    fake_module = types.ModuleType("mcp_tools")
    fake_module.OpenDentalMCPTools = FakeOpenDentalMCPTools
    monkeypatch.setitem(sys.modules, "mcp_tools", fake_module)
    yield fake_module


# ---------------------------------------------------------------------------
# Test app + sample data
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_mcp_tools) -> Flask:
    cache_db = tmp_path / "intake.db"
    monkeypatch.setattr(ic, "DEFAULT_INTAKE_DB", cache_db)

    # Drop a tiny fake source PDF on disk so /pdf endpoint has something to extract.
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    pdf_buf = io.BytesIO()
    writer.write(pdf_buf)
    pdf_path = tmp_path / "batch1.pdf"
    pdf_path.write_bytes(pdf_buf.getvalue())

    # Re-import intake_routes so it picks up the patched DEFAULT_INTAKE_DB.
    if "intake_routes" in sys.modules:
        del sys.modules["intake_routes"]
    from intake_routes import intake_bp

    flask_app = Flask(__name__)
    flask_app.register_blueprint(intake_bp)
    flask_app.config["TESTING"] = True
    flask_app.config["_pdf_path"] = pdf_path
    flask_app.config["_cache_db"] = cache_db
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def lan_headers() -> dict:
    """Use a localhost remote_addr so the LAN gate passes."""
    return {}  # default werkzeug remote_addr is 127.0.0.1, which counts as LAN


def _seed_pending(
    cache_db: Path, source_pdf: Path,
    *, status: str = "queued",
    pat_num: int | None = 100, def_num: int | None = 461,
    pages: list[int] | None = None,
) -> int:
    with ic.open_cache(cache_db) as conn:
        pid = ic.insert_pending(conn, ic.IntakePending(
            source_pdf=str(source_pdf),
            source_pdf_sha256="fakesha",
            page_indices=pages or [0, 1],
            extracted_name="Smith, Jane",
            extracted_dob="1980-04-12",
            extracted_text_len=1500,
            suggested_pat_num=pat_num,
            suggested_pat_label=f"Smith, Jane ({pat_num})" if pat_num else None,
            suggested_category="medical_history",
            suggested_def_num=def_num,
            patient_confidence=0.95 if pat_num else 0.0,
            category_confidence=0.9,
            split_confidence=0.95,
            overall_confidence=0.85,
            status=status,
        ))
        return pid


# ---------------------------------------------------------------------------
# Health + static
# ---------------------------------------------------------------------------

def test_healthz_open_to_anyone(client) -> None:
    """healthz must NOT require LAN — used by tunnel probes."""
    r = client.get("/intake/healthz", environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 200
    assert r.get_json()["service"] == "intake"


def test_lan_gate_blocks_external_ip(client) -> None:
    r = client.get("/intake/api/queue", environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 403


def test_lan_gate_allows_localhost(client) -> None:
    r = client.get("/intake/api/queue")  # default 127.0.0.1
    assert r.status_code == 200


def test_lan_gate_allows_rfc1918(client) -> None:
    r = client.get("/intake/api/queue",
                    environ_base={"REMOTE_ADDR": "192.168.127.50"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def test_categories_endpoint_lists_curated_taxonomy(client) -> None:
    r = client.get("/intake/api/categories")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
    assert {c["short_label"] for c in data} == set(tx.short_labels())


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def test_empty_queue(client) -> None:
    r = client.get("/intake/api/queue")
    assert r.status_code == 200
    data = r.get_json()
    assert data["count"] == 0
    assert data["items"] == []


def test_queue_lists_queued_items(client, app) -> None:
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="filed")
    r = client.get("/intake/api/queue?status=queued")
    data = r.get_json()
    assert data["count"] == 1
    assert data["items"][0]["status"] == "queued"


def test_queue_default_includes_pending_queued_error(client, app) -> None:
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="error")
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="filed")
    r = client.get("/intake/api/queue")
    data = r.get_json()
    statuses = {it["status"] for it in data["items"]}
    assert statuses == {"queued", "error"}


def test_queue_attaches_friendly_category_name(client, app) -> None:
    _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                   status="queued", def_num=tx.MEDICAL_HISTORY.def_num)
    r = client.get("/intake/api/queue")
    item = r.get_json()["items"][0]
    assert item["suggested_category_od_name"] == "Medical History"


# ---------------------------------------------------------------------------
# Item detail + PDF
# ---------------------------------------------------------------------------

def test_item_detail_returns_audit(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    with ic.open_cache(app.config["_cache_db"]) as conn:
        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pid, action="extracted", actor="system",
            details={"foo": "bar"},
        ))
    r = client.get(f"/intake/api/item/{pid}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["item"]["id"] == pid
    assert len(data["audit"]) == 1
    assert data["audit"][0]["action"] == "extracted"


def test_item_detail_404_unknown_id(client) -> None:
    r = client.get("/intake/api/item/99999")
    assert r.status_code == 404


def test_pdf_endpoint_returns_pdf(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                        status="queued", pages=[0, 1])
    r = client.get(f"/intake/api/item/{pid}/pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-"


def test_pdf_endpoint_410_when_source_missing(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    # Move the source PDF out from under us
    app.config["_pdf_path"].unlink()
    r = client.get(f"/intake/api/item/{pid}/pdf")
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# Patient search
# ---------------------------------------------------------------------------

def test_patient_search_requires_at_least_one_filter(client) -> None:
    r = client.get("/intake/api/patient-search")
    assert r.status_code == 400


def test_patient_search_returns_results(client) -> None:
    FakeOpenDentalMCPTools.search_response = [
        {"PatNum": 101, "LName": "Smith", "FName": "Jane",
         "Birthdate": "1980-04-12T00:00:00"},
    ]
    r = client.get("/intake/api/patient-search?lname=Smith")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["results"]) == 1
    assert data["results"][0]["pat_num"] == 101
    assert data["results"][0]["birthdate"] == "1980-04-12"


# ---------------------------------------------------------------------------
# Confirm / override / reject
# ---------------------------------------------------------------------------

def test_confirm_files_with_suggestions(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                        status="queued", pat_num=100, def_num=461)
    r = client.post(f"/intake/api/item/{pid}/confirm",
                     json={"actor": "Ben"})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["ok"] is True
    assert data["doc_num"] == 99999
    assert data["status"] == "filed"

    # Verify the upload payload + cache state.
    assert len(FakeOpenDentalMCPTools.upload_calls) == 1
    payload = FakeOpenDentalMCPTools.upload_calls[0]
    assert payload["patient_id"] == 100
    assert payload["category"] == 461

    with ic.open_cache(app.config["_cache_db"]) as conn:
        row = ic.get_pending(conn, pid)
        assert row.status == "filed"
        assert row.target_doc_num == 99999
        assert row.decided_by == "staff:Ben"


def test_confirm_409_when_already_filed(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                        status="filed")
    r = client.post(f"/intake/api/item/{pid}/confirm", json={})
    assert r.status_code == 409


def test_confirm_400_when_no_patient_assigned(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                        status="queued", pat_num=None)
    r = client.post(f"/intake/api/item/{pid}/confirm", json={})
    assert r.status_code == 400
    assert "no patient" in r.get_json()["error"].lower()


def test_confirm_404_unknown_item(client) -> None:
    r = client.post("/intake/api/item/99999/confirm", json={})
    assert r.status_code == 404


def test_confirm_502_when_od_upload_fails(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    FakeOpenDentalMCPTools.upload_response = {
        "success": False, "error": "OD network down",
    }
    r = client.post(f"/intake/api/item/{pid}/confirm", json={})
    assert r.status_code == 502
    assert "OD network down" in r.get_json()["error"]
    with ic.open_cache(app.config["_cache_db"]) as conn:
        row = ic.get_pending(conn, pid)
        assert row.status == "error"


def test_override_files_with_supplied_pat_and_def(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"],
                        status="queued", pat_num=100, def_num=461)
    r = client.post(f"/intake/api/item/{pid}/override",
                     json={"pat_num": 555, "def_num": tx.CORRESPONDENCE_CONSENTS.def_num,
                           "actor": "Ben"})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["status"] == "overridden"

    payload = FakeOpenDentalMCPTools.upload_calls[0]
    assert payload["patient_id"] == 555
    assert payload["category"] == tx.CORRESPONDENCE_CONSENTS.def_num

    with ic.open_cache(app.config["_cache_db"]) as conn:
        row = ic.get_pending(conn, pid)
        assert row.status == "overridden"
        assert row.suggested_pat_num == 555
        assert row.suggested_def_num == tx.CORRESPONDENCE_CONSENTS.def_num


def test_override_400_when_def_num_not_in_taxonomy(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    r = client.post(f"/intake/api/item/{pid}/override",
                     json={"pat_num": 555, "def_num": 99999})
    assert r.status_code == 400


def test_override_400_on_bad_payload(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    r = client.post(f"/intake/api/item/{pid}/override",
                     json={"pat_num": "oops"})
    assert r.status_code == 400


def test_reject_marks_status_no_od_call(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="queued")
    r = client.post(f"/intake/api/item/{pid}/reject",
                     json={"reason": "duplicate", "actor": "Ben"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "rejected"

    # No OD upload happened.
    assert len(FakeOpenDentalMCPTools.upload_calls) == 0
    with ic.open_cache(app.config["_cache_db"]) as conn:
        row = ic.get_pending(conn, pid)
        assert row.status == "rejected"
        assert row.error_message == "duplicate"


def test_reject_409_when_not_in_open_status(client, app) -> None:
    pid = _seed_pending(app.config["_cache_db"], app.config["_pdf_path"], status="filed")
    r = client.post(f"/intake/api/item/{pid}/reject", json={})
    assert r.status_code == 409
