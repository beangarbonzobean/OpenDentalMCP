"""Flask blueprint for the utilization dashboard at /utilization/.

Same LAN-only gate pattern as ocr_review_routes.py / intake_routes.py.

Endpoints:
    GET  /utilization/                       static HTML dashboard
    GET  /utilization/<asset>                static asset passthrough
    GET  /utilization/api/snapshot           current state JSON for the UI
    POST /utilization/api/claude-snapshot    submit raw scraped page text
                                             (driven by Claude Code session
                                              via Chrome MCP — see docs)
    GET  /utilization/healthz                readiness probe (no auth)

The Claude scrape can't be initiated by the Flask process — it requires
a Claude Code session driving Chrome MCP. The flow is:
  1. A Claude Code session loads claude.ai/settings/usage in Chrome
  2. Calls get_page_text(), POSTs result to /api/claude-snapshot
  3. Flask parses and stores
The dashboard auto-polls /api/snapshot every 10s for the latest stored value.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_from_directory

from utilization_dashboard import gpu_poller, pipeline_poller, scraper, storage

log = logging.getLogger(__name__)


STATIC_DIR = Path(os.environ.get(
    "UTILIZATION_STATIC_DIR",
    Path(__file__).resolve().parent / "static",
)).resolve()


EXTRA_LAN_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("UTILIZATION_EXTRA_LAN_CIDRS", "").split(",")
    if c.strip()
]


utilization_bp = Blueprint("utilization", __name__, url_prefix="/utilization")


# ---------------------------------------------------------------------------
# LAN gate (same pattern as ocr_review_routes)
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


@utilization_bp.before_request
def _enforce_lan_only():
    if request.endpoint and request.endpoint.endswith(".healthz"):
        return None
    ip = _client_ip()
    if not _is_lan_ip(ip):
        log.warning("utilization: blocked non-LAN request from %s -> %s", ip, request.path)
        abort(403, description="LAN-only.")
    return None


# ---------------------------------------------------------------------------
# Static / health
# ---------------------------------------------------------------------------

@utilization_bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "utilization-dashboard"})


@utilization_bp.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@utilization_bp.get("/<path:filename>")
def static_assets(filename: str):
    return send_from_directory(STATIC_DIR, filename)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@utilization_bp.get("/api/snapshot")
def api_snapshot():
    """Return latest known state of all panels for the UI."""
    return jsonify({
        "claude": storage.read_claude_latest(),
        "gpu": _live_gpu(),
        "pipeline": _live_pipeline(),
        "routing": _routing_summary(),
    })


@utilization_bp.get("/api/routing-history")
def api_routing_history():
    """Recent routing decisions for the per-call audit panel."""
    try:
        from inference_router import log_store as router_log
        return jsonify({
            "recent": router_log.recent(limit=int(request.args.get("limit", 50))),
            "histogram": router_log.histogram_24h(),
        })
    except ImportError:
        return jsonify({"recent": [], "histogram": {}, "note": "router not installed"})


@utilization_bp.post("/api/claude-snapshot-json")
def api_claude_snapshot_json():
    """Accept the structured JSON returned by claude.ai's own usage API
    (GET /api/organizations/<uuid>/usage), translate to our schema, store.

    Body shape:
      { "usage": {...}, "prepaid_credits": {...} | null }

    The "usage" payload is what /api/organizations/<uuid>/usage returns.
    "prepaid_credits" is optional but lets us fill the balance field.
    """
    body = request.get_json(silent=True) or {}
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return jsonify({"error": "missing or non-object 'usage' field"}), 400
    parsed = scraper.parse_claude_usage_json(
        usage,
        prepaid_credits=body.get("prepaid_credits"),
    )
    if "session_pct" not in parsed and "weekly_all_pct" not in parsed:
        return jsonify({
            "error": "no recognizable fields in usage payload",
            "keys_seen": sorted(usage.keys()),
        }), 422
    ts = storage.write_claude(parsed)
    return jsonify({
        "ok": True,
        "ts": ts,
        "fields_parsed": sorted(k for k in parsed.keys() if k != "raw_text"),
    })


@utilization_bp.post("/api/claude-snapshot")
def api_claude_snapshot():
    """Accept raw page text scraped from claude.ai/settings/usage and store
    a parsed snapshot. The body is the page text itself (text/plain) or
    JSON {"text": "..."}.
    """
    raw = ""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw = body.get("text", "")
    if not raw:
        raw = request.get_data(as_text=True) or ""

    raw = raw.strip()
    if not raw:
        return jsonify({"error": "empty body — POST page text or {\"text\": ...}"}), 400

    parsed = scraper.parse_claude_usage(raw)
    if "session_pct" not in parsed and "weekly_all_pct" not in parsed:
        return jsonify({
            "error": "could not parse — no session_pct or weekly_all_pct found",
            "raw_length": len(raw),
        }), 422

    ts = storage.write_claude(parsed)
    return jsonify({
        "ok": True,
        "ts": ts,
        "fields_parsed": sorted(k for k in parsed.keys() if k != "raw_text"),
    })


# ---------------------------------------------------------------------------
# Helpers — live polls (cheap, computed per request)
# ---------------------------------------------------------------------------

def _live_gpu() -> dict:
    """Current GPU snapshot. Falls back to the most recent stored value if the
    live probe fails entirely (e.g. Ollama is down)."""
    snap = gpu_poller.probe()
    if snap.get("loaded_models") is not None or snap.get("sm_pct") is not None:
        return snap
    last = storage.read_gpu_latest()
    return last or {"source": "ollama-only", "error": "ollama-unreachable"}


def _live_pipeline() -> dict:
    return pipeline_poller.probe()


def _routing_summary() -> dict:
    """Compact 24h routing summary for the snapshot endpoint."""
    try:
        from inference_router import log_store as router_log
        return router_log.histogram_24h()
    except ImportError:
        return {}
