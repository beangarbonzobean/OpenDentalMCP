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
import re
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_from_directory

from utilization_dashboard import (
    context_bundler,
    gpu_poller,
    pipeline_poller,
    projects_poller,
    projects_storage,
    scraper,
    storage,
    worktree_manager,
)

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


@utilization_bp.get("/projects/")
def projects_index():
    return send_from_directory(STATIC_DIR, "projects.html")


@utilization_bp.get("/projects/<project_id>/")
def project_detail(project_id: str):
    """Detail view for one project. The project_id is read client-side from
    the URL; the page itself is static and fetches /api/projects/<id>/detail."""
    return send_from_directory(STATIC_DIR, "project_detail.html")


@utilization_bp.get("/<path:filename>")
def static_assets(filename: str):
    return send_from_directory(STATIC_DIR, filename)


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------

@utilization_bp.get("/api/projects/snapshot")
def api_projects_snapshot():
    """Per-project health + cached next-steps for the projects panel."""
    statuses = projects_poller.status_all()
    cached = projects_storage.latest_all()
    for s in statuses:
        s["next_steps"] = cached.get(s["id"])
    return jsonify({"projects": statuses})


@utilization_bp.get("/api/projects/<project_id>/detail")
def api_project_detail(project_id: str):
    """Full per-project payload for the detail page."""
    registry = projects_poller.load_registry()
    project = next((p for p in registry if p.get("id") == project_id), None)
    if not project:
        return jsonify({"error": f"unknown project_id: {project_id}"}), 404
    status = projects_poller.status_for(project)
    next_steps = projects_storage.latest(project_id)
    investigations = projects_storage.investigations_for_project(project_id)
    proposals = projects_storage.proposals_for_project(project_id)
    return jsonify({
        "status": _status_to_dict(status),
        "next_steps": next_steps,
        "investigations": investigations,
        "proposals": proposals,
        "project_yaml": {k: v for k, v in project.items() if k != "description"},
    })


@utilization_bp.post("/api/projects/<project_id>/investigate")
def api_investigate(project_id: str):
    """Run a read-only agent investigation on a single bullet.

    Body: {"bullet_text": "..."}
    Returns: the agent's markdown report + provenance.
    """
    import hashlib

    body = request.get_json(silent=True) or {}
    bullet_text = (body.get("bullet_text") or "").strip()
    if not bullet_text:
        return jsonify({"error": "missing 'bullet_text'"}), 400

    registry = projects_poller.load_registry()
    project = next((p for p in registry if p.get("id") == project_id), None)
    if not project:
        return jsonify({"error": f"unknown project_id: {project_id}"}), 404

    bullet_hash = hashlib.sha1(bullet_text.encode("utf-8")).hexdigest()[:16]

    # Build a focused investigation prompt. The agent gets the full project
    # bundle as background plus the specific bullet to investigate.
    bundle_prompt, _ = context_bundler.build(project)
    investigation_prompt = (
        "You are investigating ONE specific recommendation for the project "
        "described below. Use the read-only tools (Read, Grep, Glob) to "
        "gather concrete evidence from the project's files, then write a "
        "short report.\n\n"
        f"Bullet under investigation:\n  > {bullet_text}\n\n"
        "Output format (markdown):\n"
        "  ### Findings\n"
        "  - 2 to 4 bullets of CONCRETE evidence you uncovered (cite file\n"
        "    paths, line numbers, log snippets, commit SHAs)\n"
        "  ### Recommendation\n"
        "  - 1 to 3 bullets of specific changes to make. If no change is\n"
        "    needed, say so explicitly.\n"
        "  ### Confidence\n"
        "  - HIGH | MEDIUM | LOW with one line explaining why.\n\n"
        "Be concise. No preamble. Do not propose changes you can't justify\n"
        "from what you actually read.\n\n"
        "--- BACKGROUND CONTEXT ---\n"
        + bundle_prompt
    )

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        return jsonify({"error": "inference_router not installed"}), 500

    # Resolve cwd to the absolute project repo path so Read/Grep/Glob target
    # the right tree.
    repo_path_rel = project.get("repo_path", "")
    project_cwd = str((projects_poller._GIT_ROOT / repo_path_rel).resolve())

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag=f"investigate:{project_id}:{bullet_hash}",
                max_output_tokens=1500,
            ),
            investigation_prompt,
            max_tokens=1500,
            timeout=240,
            allowed_tools=["Read", "Grep", "Glob"],
            cwd=project_cwd,
        )
    except Exception as e:
        log.exception("investigate dispatch failed for %s: %s", project_id, e)
        return jsonify({"error": f"dispatch failed: {type(e).__name__}: {e}"}), 502

    ts = projects_storage.write_investigation(
        project_id,
        bullet_hash,
        bullet_text,
        content=result.text,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        tools_used=["Read", "Grep", "Glob"],
        cwd=project_cwd,
    )
    return jsonify({
        "ok": True,
        "ts": ts,
        "project_id": project_id,
        "bullet_hash": bullet_hash,
        "content": result.text,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "cwd": project_cwd,
    })


@utilization_bp.post("/api/projects/<project_id>/propose")
def api_propose(project_id: str):
    """Run an agent in a fresh git worktree to draft a change for one bullet.

    Body: {"bullet_text": "...", "mode": "l2" | "l3"}
    L2: returns the proposal pending for review.
    L3: returns the proposal AND auto-applies if the agent reported HIGH
        confidence; otherwise leaves it pending and surfaces the reason.
    """
    import hashlib

    body = request.get_json(silent=True) or {}
    bullet_text = (body.get("bullet_text") or "").strip()
    mode = (body.get("mode") or "l2").lower()
    if mode not in ("l2", "l3"):
        return jsonify({"error": f"invalid mode: {mode}"}), 400
    if not bullet_text:
        return jsonify({"error": "missing 'bullet_text'"}), 400

    registry = projects_poller.load_registry()
    project = next((p for p in registry if p.get("id") == project_id), None)
    if not project:
        return jsonify({"error": f"unknown project_id: {project_id}"}), 404

    bullet_hash = hashlib.sha1(bullet_text.encode("utf-8")).hexdigest()[:16]

    # Create the worktree.
    try:
        wt = worktree_manager.create(project_id, bullet_hash)
    except Exception as e:
        log.exception("worktree create failed for %s: %s", project_id, e)
        return jsonify({"error": f"worktree setup failed: {e}"}), 500

    repo_subpath = project.get("repo_path", "")
    project_cwd = (wt.path / repo_subpath).resolve() if repo_subpath else wt.path
    if not project_cwd.exists():
        # Worktree was created from base, but the project subpath should
        # exist there already. If it doesn't the registry is wrong.
        worktree_manager.discard(wt)
        return jsonify({
            "error": f"project repo_path {repo_subpath!r} does not exist in worktree",
        }), 500

    # Build the proposal prompt.
    bundle_prompt, _ = context_bundler.build(project)
    proposal_prompt = (
        "You are drafting a code change for ONE specific recommendation in a "
        "software project. Use Read/Grep/Glob to understand the relevant "
        "files, then use Write/Edit to make the change directly. You are "
        "running inside an ISOLATED git worktree, so your edits are safe — "
        "they will not touch the main checkout until a human (or autopilot) "
        "merges them.\n\n"
        f"Bullet to act on:\n  > {bullet_text}\n\n"
        "Make the smallest correct change. Only modify files inside the "
        "current working directory tree. After editing, output a short "
        "markdown report:\n\n"
        "  ### Summary\n"
        "  - 1-3 bullets describing what you changed and why\n"
        "  ### Files modified\n"
        "  - relative path  (1 line per file)\n"
        "  ### Confidence\n"
        "  - HIGH | MEDIUM | LOW with one line explaining why. Only output\n"
        "    HIGH if you are confident the change is correct, complete, and\n"
        "    has no obvious side effects.\n\n"
        "If you decide no change is needed, do not edit any files. Output:\n"
        "  ### Summary\n  - No change needed: <reason>\n"
        "  ### Confidence\n  - HIGH (no-op)\n\n"
        "--- BACKGROUND CONTEXT ---\n"
        + bundle_prompt
    )

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        worktree_manager.discard(wt)
        return jsonify({"error": "inference_router not installed"}), 500

    # Dispatch the agent into the worktree with write tools enabled and
    # write_scope locked to the worktree root.
    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag=f"propose:{mode}:{project_id}:{bullet_hash}",
                max_output_tokens=2000,
            ),
            proposal_prompt,
            max_tokens=2000,
            timeout=420,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit"],
            cwd=str(project_cwd),
            write_scope=str(wt.path),
        )
    except Exception as e:
        log.exception("propose dispatch failed for %s: %s", project_id, e)
        ts = projects_storage.write_proposal(
            project_id, bullet_hash, bullet_text,
            mode=mode, status="failed",
            error=f"dispatch failed: {type(e).__name__}: {e}",
            worktree_path=str(wt.path), branch=wt.branch,
        )
        worktree_manager.discard(wt)
        return jsonify({"error": str(e), "ts": ts}), 502

    # Capture the diff that the agent produced.
    diff = worktree_manager.diff_against_base(wt)
    files_changed = worktree_manager.files_changed(wt)
    confidence = _extract_confidence(result.text)

    ts = projects_storage.write_proposal(
        project_id, bullet_hash, bullet_text,
        mode=mode, status="pending",
        summary=result.text,
        diff=diff,
        files_changed=files_changed,
        worktree_path=str(wt.path),
        branch=wt.branch,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
    )

    auto_applied = False
    apply_message = ""
    if mode == "l3":
        if not files_changed:
            # Nothing changed — discard the worktree and mark as applied no-op.
            worktree_manager.discard(wt)
            projects_storage.update_proposal_status(
                project_id, bullet_hash, ts,
                status="applied", clear_worktree=True,
            )
            auto_applied = True
            apply_message = "no-op: agent made no edits"
        elif confidence == "HIGH":
            ok, msg = worktree_manager.apply_to_main(wt)
            if ok:
                worktree_manager.discard(wt)
                projects_storage.update_proposal_status(
                    project_id, bullet_hash, ts,
                    status="applied", clear_worktree=True,
                )
                auto_applied = True
                apply_message = msg
            else:
                projects_storage.update_proposal_status(
                    project_id, bullet_hash, ts,
                    status="pending",  # left for manual review
                    error=msg,
                )
                apply_message = f"auto-apply failed, left pending: {msg}"
        else:
            apply_message = (
                f"L3 declined to auto-apply because confidence={confidence!r}; "
                "left pending for manual review"
            )

    return jsonify({
        "ok": True,
        "ts": ts,
        "project_id": project_id,
        "bullet_hash": bullet_hash,
        "mode": mode,
        "status": "applied" if auto_applied else "pending",
        "summary": result.text,
        "files_changed": files_changed,
        "diff": diff,
        "confidence": confidence,
        "auto_applied": auto_applied,
        "apply_message": apply_message,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "worktree_path": str(wt.path) if not auto_applied else "",
        "branch": wt.branch,
    })


@utilization_bp.post("/api/projects/<project_id>/proposals/<bullet_hash>/apply")
def api_apply_proposal(project_id: str, bullet_hash: str):
    """Merge the latest pending proposal for this bullet into main."""
    proposal = projects_storage.latest_proposal(project_id, bullet_hash)
    if not proposal:
        return jsonify({"error": "no proposal found"}), 404
    if proposal.get("status") != "pending":
        return jsonify({"error": f"proposal status is {proposal.get('status')!r}, not pending"}), 409
    wt_path = proposal.get("worktree_path", "")
    branch = proposal.get("branch", "")
    if not wt_path or not branch:
        return jsonify({"error": "proposal has no worktree to apply"}), 409

    wt = worktree_manager.Worktree(
        project_id=project_id, bullet_hash=bullet_hash,
        path=Path(wt_path), branch=branch,
    )
    ok, msg = worktree_manager.apply_to_main(wt)
    if not ok:
        projects_storage.update_proposal_status(
            project_id, bullet_hash, proposal["ts"],
            status="pending", error=msg,
        )
        return jsonify({"error": f"apply failed: {msg}"}), 500
    worktree_manager.discard(wt)
    projects_storage.update_proposal_status(
        project_id, bullet_hash, proposal["ts"],
        status="applied", clear_worktree=True,
    )
    return jsonify({"ok": True, "message": msg})


@utilization_bp.post("/api/projects/<project_id>/proposals/<bullet_hash>/discard")
def api_discard_proposal(project_id: str, bullet_hash: str):
    """Drop the latest proposal's worktree without merging."""
    proposal = projects_storage.latest_proposal(project_id, bullet_hash)
    if not proposal:
        return jsonify({"error": "no proposal found"}), 404
    wt_path = proposal.get("worktree_path", "")
    branch = proposal.get("branch", "")
    if wt_path and branch:
        wt = worktree_manager.Worktree(
            project_id=project_id, bullet_hash=bullet_hash,
            path=Path(wt_path), branch=branch,
        )
        worktree_manager.discard(wt)
    projects_storage.update_proposal_status(
        project_id, bullet_hash, proposal["ts"],
        status="discarded", clear_worktree=True,
    )
    return jsonify({"ok": True})


_CONFIDENCE_RE = re.compile(r"\b(HIGH|MEDIUM|LOW)\b")


def _extract_confidence(text: str) -> str:
    """Pull the agent's stated confidence out of the markdown report.

    Looks for the first HIGH / MEDIUM / LOW after a 'Confidence' heading or
    bullet. Falls back to the first occurrence anywhere if no header found.
    """
    if not text:
        return ""
    # Search after a 'Confidence' line for the highest-priority match.
    after_header = re.search(
        r"(?im)^[#\s\-\*]*confidence[:\s\-]*\n?(.{0,300})",
        text, re.DOTALL,
    )
    target = after_header.group(1) if after_header else text
    m = _CONFIDENCE_RE.search(target)
    return m.group(1) if m else ""


def _status_to_dict(status) -> dict:
    """Convert ProjectStatus dataclass to a plain dict (handles nested HealthCheck)."""
    from dataclasses import asdict
    return asdict(status)


@utilization_bp.post("/api/projects/<project_id>/refresh-next-steps")
def api_refresh_next_steps(project_id: str):
    """Bundle context for `project_id`, dispatch via inference_router with a
    Sonnet-preferring Profile, store the markdown result, return the new row.
    """
    registry = projects_poller.load_registry()
    project = next((p for p in registry if p.get("id") == project_id), None)
    if not project:
        return jsonify({"error": f"unknown project_id: {project_id}"}), 404

    prompt, summary = context_bundler.build(project)

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        return jsonify({"error": "inference_router not installed"}), 500

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,           # nudge toward Sonnet hint
                tag=f"project-next-steps:{project_id}",
                max_output_tokens=900,
            ),
            prompt,
            max_tokens=900,
            timeout=180,
        )
    except Exception as e:
        log.exception("dispatch failed for %s: %s", project_id, e)
        return jsonify({"error": f"dispatch failed: {type(e).__name__}: {e}"}), 502

    ts = projects_storage.write(
        project_id,
        content=result.text,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        bundle_summary={
            "repo_files_included": summary.repo_files_included,
            "memory_files_included": summary.memory_files_included,
            "git_commits_included": summary.git_commits_included,
            "log_lines_included": summary.log_lines_included,
            "total_chars": summary.total_chars,
        },
    )
    return jsonify({
        "ok": True,
        "ts": ts,
        "project_id": project_id,
        "content": result.text,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "bundle_summary": {
            "repo_files_included": summary.repo_files_included,
            "memory_files_included": summary.memory_files_included,
            "git_commits_included": summary.git_commits_included,
            "log_lines_included": summary.log_lines_included,
        },
    })


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
