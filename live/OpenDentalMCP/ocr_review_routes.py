"""
OCR Review UI — Flask blueprint at /ocr-review/.

Lets staff sweep through each overnight batch of historical-document OCR,
side-by-side with the source PDF, and either approve the OCR (it stays
in the search cache) or flag it as bad (the row is deleted and will be
re-OCR'd on the next backfill run).

Routes:
    GET  /ocr-review/                          static dashboard HTML
    GET  /ocr-review/api/summary               counts + cost stats
    GET  /ocr-review/api/queue                 list of recent OCR'd docs
    GET  /ocr-review/api/doc/<doc_num>         full text + audit metadata
    GET  /ocr-review/api/doc/<doc_num>/pdf     original PDF (re-rendered from share)
    POST /ocr-review/api/doc/<doc_num>/approve mark Reviewed=1
    POST /ocr-review/api/doc/<doc_num>/flag    DELETE the row (next backfill re-OCRs)
    GET  /ocr-review/healthz                   readiness probe (no auth)

LAN-only (RFC-1918 source-IP gate, same pattern as np_tracker / intake).
"""

from __future__ import annotations

import io
import ipaddress
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from flask import Blueprint, abort, jsonify, request, send_file, send_from_directory

from preprocessing import document_text_cache as cache
from preprocessing.path_resolver import resolve_doc_path
from preprocessing.pdf_render import render_pdf_pages


log = logging.getLogger(__name__)


APP_DIR = Path(os.environ.get(
    "OCR_REVIEW_APP_DIR",
    Path(__file__).resolve().parent / "ocr_review_app",
)).resolve()

EXTRA_LAN_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("OCR_REVIEW_EXTRA_LAN_CIDRS", "").split(",")
    if c.strip()
]


ocr_review_bp = Blueprint("ocr_review", __name__, url_prefix="/ocr-review")


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


@ocr_review_bp.before_request
def _enforce_lan_only():
    if request.endpoint and request.endpoint.endswith(".healthz"):
        return None
    ip = _client_ip()
    if not _is_lan_ip(ip):
        log.warning("ocr-review: blocked non-LAN request from %s -> %s",
                     ip, request.path)
        abort(403, description="LAN-only.")
    return None


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@ocr_review_bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "ocr-review"})


@ocr_review_bp.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@ocr_review_bp.get("/<path:filename>")
def static_assets(filename: str):
    return send_from_directory(APP_DIR, filename)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _since_iso(default_days: int = 7) -> str:
    """Resolve the recency window from a `?since=YYYY-MM-DD` query arg, falling
    back to `default_days` ago. Returns an ISO-8601 string."""
    arg = request.args.get("since", "").strip()
    if arg:
        try:
            d = datetime.fromisoformat(arg)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.isoformat()
        except ValueError:
            pass
    return (datetime.now(timezone.utc) - timedelta(days=default_days)).isoformat()


@ocr_review_bp.get("/api/summary")
def api_summary():
    since = _since_iso()
    with cache.open_cache() as conn:
        s = cache.review_summary(conn, since_iso=since)
    return jsonify(s)


@ocr_review_bp.get("/api/queue")
def api_queue():
    """List recent OCR'd docs for review.

    Query params:
      since=YYYY-MM-DD            recency window (default: 7 days)
      include_reviewed=1          include already-approved rows
      status=ok,error,...         filter by Status (comma-sep)
      doc_category=<int>          filter by DocCategory DefNum
      source=od_backfill,intake_daytime  filter by Source (comma-sep)
      limit=<int>, offset=<int>   pagination (default 200 / 0, max 500)
    """
    since = _since_iso()
    only_unreviewed = request.args.get("include_reviewed", "") not in ("1", "true", "yes")
    status_arg = request.args.get("status", "").strip()
    status_in = [s.strip() for s in status_arg.split(",") if s.strip()] or None
    source_arg = request.args.get("source", "").strip()
    source_in = [s.strip() for s in source_arg.split(",") if s.strip()] or None
    doc_cat = request.args.get("doc_category")
    doc_cat_int = int(doc_cat) if doc_cat else None
    limit = min(500, max(1, int(request.args.get("limit", "200"))))
    offset = max(0, int(request.args.get("offset", "0")))
    with cache.open_cache() as conn:
        rows = cache.list_recent_docs(
            conn, since_iso=since, only_unreviewed=only_unreviewed,
            status_in=status_in, doc_category=doc_cat_int,
            source_in=source_in,
            limit=limit, offset=offset,
        )
    return jsonify({
        "items": [_row_to_dict(r) for r in rows],
        "count": len(rows),
        "since": since,
    })


@ocr_review_bp.get("/api/doc/<int:doc_num>")
def api_doc_detail(doc_num: int):
    with cache.open_cache() as conn:
        row = cache.get_text(conn, doc_num)
    if row is None:
        return jsonify({"error": f"doc_num {doc_num} not in cache"}), 404
    return jsonify({"item": _row_to_dict(row, include_text=True)})


@ocr_review_bp.get("/api/doc/<int:doc_num>/pdf")
def api_doc_pdf(doc_num: int):
    """Render the source PDF/image so the browser can display it in an iframe.

    Two source paths:
      - Source='od_backfill' (default): resolve the on-disk path via the OD
        patient-folder convention and stream the original file.
      - Source='intake_daytime': open the batch PDF on the share, render only
        the row's PageIndex to a single-page PDF in memory, and stream that.
    """
    with cache.open_cache() as conn:
        row = cache.get_text(conn, doc_num)
    if row is None:
        return jsonify({"error": f"doc_num {doc_num} not in cache"}), 404

    if (row.Source or "od_backfill") == "intake_daytime":
        return _serve_intake_page_pdf(row)

    if not row.FileName:
        return jsonify({"error": f"doc_num {doc_num} not in cache"}), 404

    tools = _get_tools_instance()
    if tools is None:
        return jsonify({"error": "OpenDental tools unavailable"}), 503

    # Resolve the on-disk path. We need patient name to build the path —
    # query OD on demand so the rebuild script's path-resolution logic
    # stays the single source of truth.
    try:
        pat_rows = tools._query_database(
            f"SELECT LName, FName FROM patient WHERE PatNum = {int(row.PatNum)}",
            limit=1,
        )
        rows = pat_rows.get("rows", []) if isinstance(pat_rows, dict) else pat_rows
        if not rows:
            return jsonify({"error": f"patient {row.PatNum} not found"}), 404
        lname = (rows[0].get("LName") or "").strip()
        fname = (rows[0].get("FName") or "").strip()
        path = resolve_doc_path(row.PatNum, lname, fname, row.FileName)
    except Exception as e:
        log.exception("ocr-review: failed to resolve path for doc %d", doc_num)
        return jsonify({"error": f"resolve_failed: {e}"}), 500

    if not path.exists():
        return jsonify({"error": f"source file missing: {path}"}), 410

    # Stream as the original mime type. Browsers handle PDFs and JPGs natively.
    mime = "application/pdf" if str(path).lower().endswith(".pdf") else "image/jpeg"
    return send_file(str(path), mimetype=mime, as_attachment=False,
                     download_name=path.name)


def _serve_intake_page_pdf(row: cache.DocTextRow):
    """Stream a one-page PDF extracted from the intake source PDF. Uses pymupdf
    to slice just `row.PageIndex` so the browser shows the exact page the OCR
    text came from."""
    if not row.SourcePdfPath:
        return jsonify({"error": "intake row has no SourcePdfPath"}), 410
    src = Path(row.SourcePdfPath)
    if not src.exists():
        return jsonify({"error": f"source file missing: {src}"}), 410
    page_idx = int(row.PageIndex or 0)

    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception as e:
        return jsonify({"error": f"pymupdf unavailable: {e}"}), 500

    try:
        doc = pymupdf.open(str(src))
    except Exception as e:
        return jsonify({"error": f"open_failed: {e}"}), 500

    try:
        if page_idx < 0 or page_idx >= doc.page_count:
            return jsonify({
                "error": f"page_index {page_idx} out of range (0..{doc.page_count - 1})",
            }), 410
        out = pymupdf.open()
        out.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        buf = io.BytesIO(out.tobytes())
        out.close()
    finally:
        doc.close()

    download_name = f"{src.stem}_page{page_idx + 1}.pdf"
    return send_file(
        buf, mimetype="application/pdf", as_attachment=False,
        download_name=download_name,
    )


@ocr_review_bp.post("/api/doc/<int:doc_num>/approve")
def api_doc_approve(doc_num: int):
    body = request.get_json(silent=True) or {}
    reviewer = (body.get("reviewer") or "").strip() or "staff"
    with cache.open_cache() as conn:
        ok = cache.mark_reviewed(conn, doc_num, reviewer=reviewer)
    if not ok:
        return jsonify({"error": f"doc_num {doc_num} not found"}), 404
    return jsonify({"ok": True, "status": "approved", "doc_num": doc_num,
                     "reviewer": reviewer})


@ocr_review_bp.post("/api/doc/<int:doc_num>/unapprove")
def api_doc_unapprove(doc_num: int):
    """Reverse an approval — puts the row back into the queue."""
    with cache.open_cache() as conn:
        ok = cache.unmark_reviewed(conn, doc_num)
    if not ok:
        return jsonify({"error": f"doc_num {doc_num} not found"}), 404
    return jsonify({"ok": True, "status": "unapproved", "doc_num": doc_num})


@ocr_review_bp.post("/api/doc/<int:doc_num>/flag")
def api_doc_flag(doc_num: int):
    """Flag the OCR as bad — DELETE the row. Next backfill re-OCRs the doc.
    The source PDF on the share is untouched."""
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip() or "bad_ocr"
    log.warning("ocr-review: deleting doc_num=%d for re-OCR: %s", doc_num, reason)
    with cache.open_cache() as conn:
        ok = cache.delete_doc_text(conn, doc_num)
    if not ok:
        return jsonify({"error": f"doc_num {doc_num} not found"}), 404
    return jsonify({"ok": True, "status": "deleted", "doc_num": doc_num,
                     "reason": reason})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: cache.DocTextRow, include_text: bool = False) -> dict:
    d = asdict(row)
    if not include_text:
        # The queue listing doesn't need the full text — just a snippet.
        text = d.pop("Text", "") or ""
        d["text_preview"] = (text[:240] + "…") if len(text) > 240 else text
        d["text_length"] = len(text)
    return d


def _get_tools_instance():
    try:
        import mcp_tools
        return mcp_tools.OpenDentalMCPTools()
    except Exception as e:
        log.exception("ocr-review: failed to instantiate OpenDentalMCPTools: %s", e)
        return None
