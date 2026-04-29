"""
New Patient Tracker — Flask blueprint.

Mounts under the existing OpenDentalMCP Flask app at `/tracker`:

    GET  /tracker/                    -> serves the static dashboard HTML
    GET  /tracker/api/new-patients    -> JSON, wraps get_new_patient_exam_doctors()
    GET  /tracker/healthz             -> readiness probe (no auth)

Access model: LAN-only. All /tracker/* routes are gated by an RFC-1918 source-IP
check (see _is_lan_ip). The Cloudflare tunnel will still forward these paths if
they aren't excluded in tunnel config, but external requests will be 403'd at
the Flask layer regardless.

Wiring (3 edits in the existing OpenDentalMCP server) — see INTEGRATION.md.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from typing import Optional

from flask import Blueprint, abort, jsonify, request, send_from_directory

# Reuse the existing resolver — it lives next to mcp_tools.py on the server.
# DO NOT duplicate the SQL or note-parsing logic here.
from new_patient_doctor_resolver import get_new_patient_exam_doctors

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path to the static folder containing index.html. Defaults to a
# `np_tracker_app/` directory in the SAME folder as this .py file — that's
# the layout HANDOFF.md instructs Opus to deploy. Override with
# NP_TRACKER_APP_DIR env var if you stage the static assets elsewhere.
APP_DIR = Path(os.environ.get(
    "NP_TRACKER_APP_DIR",
    Path(__file__).resolve().parent / "np_tracker_app",
)).resolve()

# Extra LAN ranges beyond RFC-1918 (e.g. a VPN subnet). Comma-separated CIDRs.
EXTRA_LAN_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("NP_TRACKER_EXTRA_LAN_CIDRS", "").split(",")
    if c.strip()
]

# Hard date-range cap so a stray client can't ask for "the last 10 years".
MAX_RANGE_DAYS = int(os.environ.get("NP_TRACKER_MAX_RANGE_DAYS", "366"))

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

np_tracker_bp = Blueprint(
    "np_tracker",
    __name__,
    url_prefix="/tracker",
)


# ---------------------------------------------------------------------------
# LAN-only gate
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    """Return the originating client IP, honoring X-Forwarded-For if present.

    Cloudflare tunnels populate X-Forwarded-For. If you trust Cloudflare in
    front of this app, the leftmost entry is the real client IP.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _is_lan_ip(ip_str: str) -> bool:
    """True if ip_str is RFC-1918 / loopback / link-local / our extra CIDRs."""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    return any(ip in net for net in EXTRA_LAN_CIDRS)


@np_tracker_bp.before_request
def _enforce_lan_only():
    # Allow the unauth'd healthz from anywhere — useful for tunnel probes.
    if request.endpoint and request.endpoint.endswith(".healthz"):
        return None
    ip = _client_ip()
    if not _is_lan_ip(ip):
        log.warning("np_tracker: blocked non-LAN request from %s -> %s",
                    ip, request.path)
        abort(403, description="LAN-only.")
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@np_tracker_bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "np-tracker"})


@np_tracker_bp.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@np_tracker_bp.get("/<path:filename>")
def static_assets(filename: str):
    # In case you split the dashboard into multiple files later.
    # Refuses path traversal automatically via send_from_directory.
    return send_from_directory(APP_DIR, filename)


@np_tracker_bp.get("/api/new-patients")
def api_new_patients():
    """Wrap get_new_patient_exam_doctors() and return JSON.

    Query params:
        from (YYYY-MM-DD, required, inclusive)
        to   (YYYY-MM-DD, required, inclusive)
        include_note_text=1 (optional, debug)
    """
    from_date = request.args.get("from", "").strip()
    to_date   = request.args.get("to", "").strip()
    include_note = request.args.get("include_note_text") in ("1", "true", "yes")

    err = _validate_range(from_date, to_date)
    if err:
        return jsonify({"error": err}), 400

    tools = _get_tools_instance()
    if tools is None:
        log.error("np_tracker: OpenDentalTools instance unavailable")
        return jsonify({"error": "OpenDental tools unavailable"}), 503

    try:
        rows = get_new_patient_exam_doctors(
            tools, from_date, to_date, include_note_text=include_note,
        )
    except Exception as e:
        log.exception("np_tracker: resolver failed")
        return jsonify({"error": f"resolver failed: {e}"}), 500

    return jsonify(rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import re
from datetime import date

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_range(from_date: str, to_date: str) -> Optional[str]:
    if not _DATE_RE.match(from_date or ""):
        return "from must be YYYY-MM-DD"
    if not _DATE_RE.match(to_date or ""):
        return "to must be YYYY-MM-DD"
    try:
        d_from = date.fromisoformat(from_date)
        d_to   = date.fromisoformat(to_date)
    except ValueError as e:
        return f"invalid date: {e}"
    if d_from > d_to:
        return "from is after to"
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        return f"range exceeds NP_TRACKER_MAX_RANGE_DAYS ({MAX_RANGE_DAYS})"
    return None


def _get_tools_instance():
    """Return the OpenDentalTools singleton the MCP server already constructs.

    NOTE FOR CLAUDE CODE: the existing server holds an OpenDentalTools
    instance somewhere — most likely a module-level singleton in mcp_tools.py
    or constructed in the Flask app factory. Wire this function to return
    that same instance. Two common shapes:

        # If mcp_tools.py exposes a module-level `tools`:
        from mcp_tools import tools
        return tools

        # If the Flask app stashes it on `app.extensions` or `app.config`:
        from flask import current_app
        return current_app.config.get("OPEN_DENTAL_TOOLS")

    Pick whichever matches the existing pattern. Don't construct a second
    instance — they share connection pools / auth state.
    """
    # Singleton is `tools = OpenDentalMCPTools()` at module load in
    # mcp_server_http.py. Lazy import avoids a circular dependency since
    # mcp_server_http imports this blueprint.
    try:
        from mcp_server_http import tools  # type: ignore[attr-defined]
        return tools
    except Exception:
        return None
