"""
Intake Review UI — Flask blueprint.

Mounts under the existing OpenDentalMCP Flask app at `/intake`:

    GET  /intake/                     -> serves the static review dashboard HTML
    GET  /intake/api/queue            -> JSON list of pending/queued/error items
    GET  /intake/api/item/<id>        -> JSON details for one item + audit log
    GET  /intake/api/item/<id>/pdf    -> the candidate PDF (extracted pages)
    POST /intake/api/item/<id>/confirm  -> file with current suggested patient/category
    POST /intake/api/item/<id>/override -> file with overridden patient/category
    POST /intake/api/item/<id>/reject -> mark rejected (no OD write)
    GET  /intake/api/categories       -> curated taxonomy as JSON
    GET  /intake/api/patient-search   -> proxy to OD search_patients (LName/FName/DOB)
    GET  /intake/healthz              -> readiness probe (no auth)

Access model: LAN-only — same RFC-1918 source-IP gate as np_tracker. The
filer module is the only place that performs OD writes; this blueprint
forwards staff confirmations into it.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, abort, jsonify, request, send_file, send_from_directory

from preprocessing.intake import cache as ic
from preprocessing.intake import filer as filer_mod
from preprocessing.intake import taxonomy as tx


log = logging.getLogger(__name__)


APP_DIR = Path(os.environ.get(
    "INTAKE_APP_DIR",
    Path(__file__).resolve().parent / "intake_app",
)).resolve()

EXTRA_LAN_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("INTAKE_EXTRA_LAN_CIDRS", "").split(",")
    if c.strip()
]


intake_bp = Blueprint("intake", __name__, url_prefix="/intake")


# ---------------------------------------------------------------------------
# LAN-only gate
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _is_lan_ip(ip_str: str) -> bool:
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    return any(ip in net for net in EXTRA_LAN_CIDRS)


@intake_bp.before_request
def _enforce_lan_only():
    if request.endpoint and request.endpoint.endswith(".healthz"):
        return None
    ip = _client_ip()
    if not _is_lan_ip(ip):
        log.warning("intake: blocked non-LAN request from %s -> %s", ip, request.path)
        abort(403, description="LAN-only.")
    return None


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@intake_bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "intake"})


@intake_bp.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@intake_bp.get("/<path:filename>")
def static_assets(filename: str):
    return send_from_directory(APP_DIR, filename)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@intake_bp.get("/api/categories")
def api_categories():
    return jsonify([
        {
            "def_num": c.def_num,
            "od_name": c.od_name,
            "short_label": c.short_label,
            "description": c.description,
        }
        for c in tx.ALL_CATEGORIES
    ])


@intake_bp.get("/api/queue")
def api_queue():
    """List items by status. Defaults to queued+pending+error."""
    statuses_arg = request.args.get("status", "queued,pending,error")
    statuses = [s.strip() for s in statuses_arg.split(",") if s.strip()]
    limit = min(500, max(1, int(request.args.get("limit", "200"))))

    out: list[dict] = []
    with ic.open_cache() as conn:
        for status in statuses:
            if status not in ic.VALID_STATUS:
                continue
            rows = ic.list_by_status(conn, status, limit=limit)
            for r in rows:
                out.append(_pending_to_dict(r))
    out.sort(key=lambda r: r.get("discovered_at") or "", reverse=True)
    return jsonify({"items": out[:limit], "count": len(out)})


@intake_bp.get("/api/item/<int:pending_id>")
def api_item_detail(pending_id: int):
    with ic.open_cache() as conn:
        row = ic.get_pending(conn, pending_id)
        if row is None:
            return jsonify({"error": f"item {pending_id} not found"}), 404
        audit = ic.list_audit_for_pending(conn, pending_id)
    return jsonify({
        "item": _pending_to_dict(row),
        "audit": audit,
    })


@intake_bp.get("/api/item/<int:pending_id>/pdf")
def api_item_pdf(pending_id: int):
    """Return the extracted PDF (only the pages belonging to this candidate)."""
    with ic.open_cache() as conn:
        row = ic.get_pending(conn, pending_id)
    if row is None:
        return jsonify({"error": f"item {pending_id} not found"}), 404
    src_path = Path(row.source_pdf)
    if not src_path.exists():
        return jsonify({"error": f"source pdf missing: {src_path}"}), 410
    try:
        src_bytes = src_path.read_bytes()
        out_pdf = filer_mod._extract_pages_to_pdf(src_bytes, row.page_indices)
    except Exception as e:
        log.exception("intake: pdf extraction failed for item %d", pending_id)
        return jsonify({"error": f"pdf_extract_failed: {e}"}), 500
    return send_file(
        io.BytesIO(out_pdf),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"intake_{pending_id}.pdf",
    )


@intake_bp.get("/api/patient-search")
def api_patient_search():
    """Proxy to OD search_patients for the patient-override picker.

    Query params:
      lname (optional) — surname filter
      fname (optional) — given name filter
      dob   (optional) — YYYY-MM-DD
    """
    tools = _get_tools_instance()
    if tools is None:
        return jsonify({"error": "OpenDental tools unavailable"}), 503
    # tools._search_patients translates snake_case input keys
    # (last_name/first_name/birthdate) to OD's CamelCase. Calling with
    # the CamelCase keys directly silently drops the filter and OD
    # returns all 1000 patients.
    params: dict = {}
    if request.args.get("lname"):
        params["last_name"] = request.args["lname"].strip()
    if request.args.get("fname"):
        params["first_name"] = request.args["fname"].strip()
    if request.args.get("dob"):
        params["birthdate"] = request.args["dob"].strip()
    if not params:
        return jsonify({"error": "at least one of lname / fname / dob required"}), 400
    try:
        raw = tools._search_patients(params)
    except Exception as e:
        log.exception("intake: patient search failed")
        return jsonify({"error": f"search_failed: {e}"}), 500
    rows = _coerce_patient_rows(raw)[:50]
    return jsonify({
        "results": [
            {
                "pat_num": int(r.get("PatNum")),
                "lname": r.get("LName"),
                "fname": r.get("FName"),
                "birthdate": (r.get("Birthdate") or "")[:10],
                "label": f'{r.get("LName")}, {r.get("FName")} ({r.get("PatNum")})',
            }
            for r in rows if r.get("PatNum") is not None
        ],
    })


@intake_bp.post("/api/item/<int:pending_id>/confirm")
def api_item_confirm(pending_id: int):
    """File the document using the current suggested patient + category."""
    return _file_item(pending_id, override_pat_num=None, override_def_num=None)


@intake_bp.post("/api/item/<int:pending_id>/override")
def api_item_override(pending_id: int):
    """File the document with staff overrides.

    JSON body: {pat_num: int, def_num: int}
    """
    body = request.get_json(silent=True) or {}
    try:
        pat_num = int(body.get("pat_num"))
        def_num = int(body.get("def_num"))
    except (TypeError, ValueError):
        return jsonify({"error": "pat_num and def_num must be integers"}), 400
    if def_num not in tx.def_nums():
        return jsonify({"error": f"def_num {def_num} is not in the curated taxonomy"}), 400
    return _file_item(pending_id, override_pat_num=pat_num, override_def_num=def_num)


@intake_bp.post("/api/item/<int:pending_id>/reject")
def api_item_reject(pending_id: int):
    """Mark the item rejected. No OD write. Source file is not moved."""
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip() or "rejected_by_staff"
    actor = (body.get("actor") or "").strip() or "staff"

    with ic.open_cache() as conn:
        row = ic.get_pending(conn, pending_id)
        if row is None:
            return jsonify({"error": f"item {pending_id} not found"}), 404
        if row.status not in ("queued", "pending", "error"):
            return jsonify({"error": f"item is in status {row.status}; cannot reject"}), 409
        ic.update_pending_status(
            conn, pending_id, status="rejected",
            error_message=reason, decided_by=f"staff:{actor}",
        )
        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pending_id, action="rejected", actor=f"staff:{actor}",
            details={"reason": reason},
        ))
    return jsonify({"ok": True, "status": "rejected"})


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _file_item(
    pending_id: int,
    *,
    override_pat_num: Optional[int],
    override_def_num: Optional[int],
) -> Any:
    body = request.get_json(silent=True) or {}
    actor = (body.get("actor") or "").strip() or "staff"

    tools = _get_tools_instance()
    if tools is None:
        return jsonify({"error": "OpenDental tools unavailable"}), 503

    with ic.open_cache() as conn:
        row = ic.get_pending(conn, pending_id)
        if row is None:
            return jsonify({"error": f"item {pending_id} not found"}), 404
        if row.status not in ("queued", "pending", "error"):
            return jsonify({"error": f"item is in status {row.status}; cannot file"}), 409

        pat_num = override_pat_num if override_pat_num is not None else row.suggested_pat_num
        def_num = override_def_num if override_def_num is not None else row.suggested_def_num
        if not pat_num:
            return jsonify({"error": "no patient assigned; use /override"}), 400
        if not def_num:
            return jsonify({"error": "no DocCategory assigned; use /override"}), 400

        src_path = Path(row.source_pdf)
        if not src_path.exists():
            return jsonify({"error": f"source pdf missing: {src_path}"}), 410
        src_bytes = src_path.read_bytes()

        is_override = (
            override_pat_num is not None or override_def_num is not None
        )
        action_label = "overridden" if is_override else "filed"

        result = filer_mod.file_document(
            source_pdf_bytes=src_bytes,
            page_indices=row.page_indices,
            pat_num=pat_num,
            def_num=def_num,
            description=f"Filed via review UI ({action_label})",
            file_name_hint=_filename_for(row, def_num),
            od_uploader=tools._upload_document,
        )
        if not result.success:
            ic.update_pending_status(
                conn, pending_id, status="error",
                error_message=result.error, decided_by=f"staff:{actor}",
            )
            ic.write_audit(conn, ic.IntakeAudit(
                pending_id=pending_id, action="error", actor=f"staff:{actor}",
                details={"error": result.error, "intended_action": action_label},
            ))
            return jsonify({"ok": False, "error": result.error}), 502

        ic.update_pending_status(
            conn, pending_id,
            status=action_label,
            target_doc_num=result.doc_num,
            target_file_path=result.file_path,
            suggested_pat_num=pat_num,
            suggested_def_num=def_num,
            decided_by=f"staff:{actor}",
        )
        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pending_id, action=action_label, actor=f"staff:{actor}",
            details={
                "doc_num": result.doc_num,
                "file_path": result.file_path,
                "pat_num": pat_num,
                "def_num": def_num,
                "override_pat_num": override_pat_num,
                "override_def_num": override_def_num,
            },
        ))

    return jsonify({
        "ok": True,
        "status": action_label,
        "doc_num": result.doc_num,
        "file_path": result.file_path,
    })


def _filename_for(row: ic.IntakePending, def_num: int) -> str:
    cat = next((c for c in tx.ALL_CATEGORIES if c.def_num == def_num), tx.MISCELLANEOUS)
    name_part = (row.extracted_name or row.suggested_pat_label or "unknown")
    return f"{cat.short_label}_{name_part}".replace(",", "").replace(" ", "_") + ".pdf"


def _pending_to_dict(row: ic.IntakePending) -> dict:
    d = asdict(row)
    # Add a friendly category label.
    if row.suggested_def_num:
        cat = next(
            (c for c in tx.ALL_CATEGORIES if c.def_num == row.suggested_def_num),
            None,
        )
        if cat:
            d["suggested_category_od_name"] = cat.od_name
    return d


def _coerce_patient_rows(raw: Any) -> list[dict]:
    """Reuse the same coercion shape as patient_matcher."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for key in ("patients", "results", "data", "rows"):
            v = raw.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        if "PatNum" in raw:
            return [raw]
    return []


def _get_tools_instance():
    """Return the live OpenDentalMCPTools singleton.

    The MCP server constructs one when handling JSON-RPC requests. We
    construct a fresh one here per request — same env, same auth — which
    is cheap because it only opens a requests.Session.
    """
    try:
        import mcp_tools
        return mcp_tools.OpenDentalMCPTools()
    except Exception as e:
        log.exception("intake: failed to instantiate OpenDentalMCPTools: %s", e)
        return None
