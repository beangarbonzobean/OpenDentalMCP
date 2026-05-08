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
    async_runner,
    context_bundler,
    gpu_poller,
    manager_bundler,
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


@utilization_bp.get("/api/manager/snapshot")
def api_manager_snapshot():
    """Latest manager's brief + metadata for the projects-page header."""
    return jsonify({"brief": projects_storage.latest_manager_brief()})


MANAGER_OPUS_MODEL = os.environ.get("ROUTER_OPUS_MODEL", "claude-opus-4-1")


def _run_user_idea_in_background(idea_id: int, idea: str) -> None:
    """Background worker: bundle context, ask Opus to produce a polished
    bullet from the user's idea, store it linked to the idea row."""
    import hashlib
    import re as _re

    try:
        prompt, _ = manager_bundler.build_user_idea_prompt(idea)
    except Exception as e:
        projects_storage.update_user_idea(
            idea_id, status="failed",
            error=f"bundle failed: {type(e).__name__}: {e}",
        )
        return

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        projects_storage.update_user_idea(
            idea_id, status="failed",
            error="inference_router not installed",
        )
        return

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag=f"user-idea:{idea_id}",
                max_output_tokens=600,
            ),
            prompt,
            max_tokens=600,
            timeout=240,
            model_hint=MANAGER_OPUS_MODEL,    # Opus for consistent voice
        )
    except Exception as e:
        projects_storage.update_user_idea(
            idea_id, status="failed",
            error=f"dispatch failed: {type(e).__name__}: {e}",
        )
        return

    # Parse the response: "### Section\n- bullet text"
    text = (result.text or "").strip()
    section_m = _re.match(r"^#{1,4}\s+(.+?)\n", text)
    section = section_m.group(1).strip() if section_m else "User-submitted ideas"
    bullet_m = _re.search(r"\n\s*[-*]\s+(.+)$", text, _re.DOTALL)
    bullet = (bullet_m.group(1).strip() if bullet_m else text).strip()
    if not bullet:
        projects_storage.update_user_idea(
            idea_id, status="failed",
            error="no bullet produced", model_used=result.model,
            provider_used=result.provider, latency_ms=result.latency_ms,
            cost_usd=result.cost_usd,
        )
        return

    bullet_hash = hashlib.sha1(bullet.encode("utf-8")).hexdigest()[:16]
    projects_storage.update_user_idea(
        idea_id, status="ok", section=section, bullet=bullet,
        bullet_hash=bullet_hash,
        model_used=result.model, provider_used=result.provider,
        latency_ms=result.latency_ms, cost_usd=result.cost_usd,
    )


@utilization_bp.post("/api/manager/submit-idea")
def api_submit_idea():
    """Accept a free-form idea from the user, dispatch Opus to fold it into
    the brief format, return immediately with the new idea's id. UI polls
    /api/manager/actions for the bullet to appear."""
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    if not idea:
        return jsonify({"error": "missing 'idea' (non-empty string required)"}), 400
    if len(idea) > 4000:
        return jsonify({"error": "idea too long (max 4000 chars)"}), 400

    idea_id = projects_storage.write_user_idea(idea)
    async_runner.submit(_run_user_idea_in_background, idea_id, idea)
    return jsonify({
        "ok": True,
        "queued": True,
        "idea_id": idea_id,
        "message": "Manager processing your idea — poll /api/manager/actions for the bullet.",
    }), 202


@utilization_bp.get("/api/manager/actions")
def api_manager_actions():
    """Return all latest per-bullet actions + manager-originated proposals
    so the UI can attach results to the right bullets when re-rendering the
    brief.

    Shape: {
      "actions":   {bullet_hash: {action: row, ...}, ...},
      "proposals": {bullet_hash: {project_id: row, ...}, ...},
      "registry":  [{id, name, domain}, ...]    # for the project picker
    }
    """
    registry = [
        {"id": p.get("id"), "name": p.get("name"), "domain": p.get("domain", "")}
        for p in projects_poller.load_registry()
    ]
    return jsonify({
        "actions": projects_storage.all_manager_actions(),
        "proposals": projects_storage.manager_proposals_by_bullet(),
        "registry": registry,
        "user_ideas": projects_storage.list_user_ideas(limit=50),
    })


@utilization_bp.post("/api/manager/refresh")
def api_manager_refresh():
    """Bundle all-project state and ask OPUS to produce a portfolio brief.
    Opus is preferred over Sonnet for portfolio-level reasoning — slower
    but considers the relationships between projects more carefully."""
    prompt, summary = manager_bundler.build()

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        return jsonify({"error": "inference_router not installed"}), 500

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag="manager-brief",
                max_output_tokens=2000,
            ),
            prompt,
            max_tokens=2000,
            timeout=420,                      # Opus runs slower; allow more
            model_hint=MANAGER_OPUS_MODEL,    # explicit Opus override
        )
    except Exception as e:
        log.exception("manager refresh dispatch failed: %s", e)
        return jsonify({"error": f"dispatch failed: {type(e).__name__}: {e}"}), 502

    ts = projects_storage.write_manager_brief(
        content=result.text,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        projects_in_bundle=summary.project_ids,
        bundle_chars=summary.total_chars,
    )
    return jsonify({
        "ok": True,
        "ts": ts,
        "content": result.text,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "projects_in_bundle": summary.project_ids,
        "bundle_chars": summary.total_chars,
    })


def _run_manager_action_in_background(
    bullet_hash: str,
    action: str,
    bullet_text: str,
    section: str,
    running_ts: str,
) -> None:
    """Background worker that does the actual dispatch + status update."""
    try:
        prompt, _ = manager_bundler.build_action_prompt(
            action, bullet_text, section=section,
        )
    except ValueError as e:
        projects_storage.write_manager_action(
            bullet_hash, action, bullet_text,
            section=section, status="failed", error=str(e), ts=running_ts,
        )
        return

    allowed_tools = ["Read", "Grep", "Glob"] if action == "investigate" else None
    cwd = str(projects_poller._GIT_ROOT) if action == "investigate" else None

    try:
        from inference_router import Profile, dispatch
    except ImportError:
        projects_storage.write_manager_action(
            bullet_hash, action, bullet_text,
            section=section, status="failed",
            error="inference_router not installed", ts=running_ts,
        )
        return

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag=f"manager-action:{action}:{bullet_hash}",
                max_output_tokens=1500,
            ),
            prompt,
            max_tokens=1500,
            timeout=240,
            allowed_tools=allowed_tools,
            cwd=cwd,
        )
    except Exception as e:
        projects_storage.write_manager_action(
            bullet_hash, action, bullet_text,
            section=section, status="failed",
            error=f"{type(e).__name__}: {e}", ts=running_ts,
        )
        return

    projects_storage.write_manager_action(
        bullet_hash, action, bullet_text,
        section=section, status="ok",
        content=result.text,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        ts=running_ts,
    )


@utilization_bp.post("/api/manager/bullets/<bullet_hash>/<action>")
def api_manager_bullet_action(bullet_hash: str, action: str):
    """Run a per-bullet manager action (plan / investigate) and return the
    agent's report. Body: {"bullet_text": "...", "section": "..."}.

    Both supported actions are read-only L1-style. They use Sonnet (cheaper
    than Opus, fast enough for tactical drilldowns) and dispatch through
    the inference router so the cost shows up in the dashboard.
    """
    if action not in ("plan", "investigate"):
        return jsonify({"error": f"unknown action: {action}"}), 400
    body = request.get_json(silent=True) or {}
    bullet_text = (body.get("bullet_text") or "").strip()
    section = (body.get("section") or "").strip()
    if not bullet_text:
        return jsonify({"error": "missing 'bullet_text'"}), 400

    # Write 'running' row, then submit dispatch to a background thread
    # and return immediately so the browser doesn't hang for 30-90s.
    running_ts = projects_storage.write_manager_action(
        bullet_hash, action, bullet_text,
        section=section, status="running",
    )
    async_runner.submit(
        _run_manager_action_in_background,
        bullet_hash, action, bullet_text, section, running_ts,
    )
    return jsonify({
        "ok": True,
        "queued": True,
        "ts": running_ts,
        "bullet_hash": bullet_hash,
        "action": action,
        "message": f"{action} dispatched in background; poll /api/manager/actions",
    }), 202


def _run_draft_poc_in_background(
    bullet_hash: str,
    bullet_text: str,
    project_id: str,
    project: dict,
    wt,
    project_cwd: str,
    running_ts: str,
) -> None:
    """Background worker for manager-originated Draft PoC.

    Same agent setup as L2 proposal (Read/Grep/Glob/Write/Edit, write_scope
    locked to worktree) but the prompt is framed around a manager bullet
    + a single chosen project rather than a per-project tactical bullet.
    """
    try:
        from inference_router import Profile, dispatch
    except ImportError:
        projects_storage.write_proposal(
            project_id, bullet_hash, bullet_text,
            mode="manager-l2", status="failed",
            error="inference_router not installed",
            worktree_path=str(wt.path), branch=wt.branch,
            ts=None,  # new ts since the running row has the same key
        )
        worktree_manager.discard(wt)
        return

    bundle_prompt, _ = context_bundler.build(project)
    poc_prompt = (
        "You are drafting a proof-of-concept code change for ONE project, "
        "targeting a specific RECOMMENDATION made at the portfolio level. "
        "You are running inside an ISOLATED git worktree of the project's "
        "repo, so your edits are safe — they will not touch main until "
        "merged.\n\n"
        f"Portfolio-level recommendation:\n  > {bullet_text}\n\n"
        f"Target project for this PoC:\n  > {project.get('name')} ({project_id})\n\n"
        "Make the smallest, most representative change that demonstrates "
        "this recommendation in this ONE project. Other affected projects "
        "are not your concern here — keep the change minimal and confined "
        "to this project's repo.\n\n"
        "After editing, output a markdown report with:\n"
        "  ### Summary\n  - 1-3 bullets: what you changed and why this "
        "demonstrates the recommendation\n"
        "  ### Files modified\n  - relative path per file\n"
        "  ### How this validates the manager's recommendation\n  - 1-2 "
        "sentences linking the change back to the broader portfolio idea\n"
        "  ### Confidence\n  - HIGH | MEDIUM | LOW with one line. Only "
        "HIGH if the change is correct, complete, and a faithful "
        "demonstration.\n\n"
        "If no change makes sense for this project (the recommendation "
        "doesn't actually apply here), output:\n"
        "  ### Summary\n  - This project doesn't need the change because: <reason>\n"
        "  ### Confidence\n  - HIGH (no-op)\n\n"
        "--- BACKGROUND CONTEXT (this project) ---\n"
        + bundle_prompt
    )

    try:
        result = dispatch(
            Profile(
                fits_local=False,
                prefers_high_end=True,
                tag=f"draft-poc:{project_id}:{bullet_hash}",
                max_output_tokens=2000,
            ),
            poc_prompt,
            max_tokens=2000,
            timeout=420,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit"],
            cwd=project_cwd,
            write_scope=str(wt.path),
        )
    except Exception as e:
        log.exception("draft-poc dispatch failed for %s: %s", project_id, e)
        projects_storage.write_proposal(
            project_id, bullet_hash, bullet_text,
            mode="manager-l2", status="failed",
            error=f"{type(e).__name__}: {e}",
            worktree_path=str(wt.path), branch=wt.branch,
            ts=None,
        )
        worktree_manager.discard(wt)
        return

    diff = worktree_manager.diff_against_base(wt)
    files_changed = worktree_manager.files_changed(wt)

    projects_storage.write_proposal(
        project_id, bullet_hash, bullet_text,
        mode="manager-l2", status="pending",
        summary=result.text,
        diff=diff,
        files_changed=files_changed,
        worktree_path=str(wt.path),
        branch=wt.branch,
        model_used=result.model,
        provider_used=result.provider,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        ts=None,
    )


@utilization_bp.post("/api/manager/bullets/<bullet_hash>/draft-poc/<project_id>")
def api_manager_draft_poc(bullet_hash: str, project_id: str):
    """Manager-originated L2 proposal: agent edits ONE project to demonstrate
    a portfolio-level recommendation.

    Body: {"bullet_text": "..."}
    Returns 202 with a 'queued' result; UI polls /api/projects/<id>/detail
    (proposal_for_project) to see the result. The proposal lands in the
    same project_proposal table as per-project L2 proposals; users can
    Apply/Discard from the project's detail page.
    """
    body = request.get_json(silent=True) or {}
    bullet_text = (body.get("bullet_text") or "").strip()
    if not bullet_text:
        return jsonify({"error": "missing 'bullet_text'"}), 400

    registry = projects_poller.load_registry()
    project = next((p for p in registry if p.get("id") == project_id), None)
    if not project:
        return jsonify({"error": f"unknown project_id: {project_id}"}), 404

    # Worktree setup is synchronous — fast (~1s). Failures here surface
    # immediately to the UI rather than confusing the user later.
    try:
        wt = worktree_manager.create(project_id, bullet_hash)
    except Exception as e:
        log.exception("draft-poc worktree create failed: %s", e)
        return jsonify({"error": f"worktree setup failed: {e}"}), 500

    repo_subpath = project.get("repo_path", "")
    project_cwd = (wt.path / repo_subpath).resolve() if repo_subpath else wt.path
    if not project_cwd.exists():
        worktree_manager.discard(wt)
        return jsonify({
            "error": f"project repo_path {repo_subpath!r} does not exist in worktree",
        }), 500

    # Write 'running' proposal row so UI can show "Drafting…"
    running_ts = projects_storage.write_proposal(
        project_id, bullet_hash, bullet_text,
        mode="manager-l2", status="running",
        worktree_path=str(wt.path), branch=wt.branch,
    )
    async_runner.submit(
        _run_draft_poc_in_background,
        bullet_hash, bullet_text, project_id, project, wt, str(project_cwd), running_ts,
    )

    return jsonify({
        "ok": True,
        "queued": True,
        "ts": running_ts,
        "project_id": project_id,
        "bullet_hash": bullet_hash,
        "worktree_path": str(wt.path),
        "branch": wt.branch,
        "message": "Draft PoC dispatched in background. Poll /api/projects/<id>/detail.",
    }), 202


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
