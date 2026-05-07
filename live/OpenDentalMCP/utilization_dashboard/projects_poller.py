"""Per-project health probe + metadata loader for the projects panel.

For each project in projects.yaml, gather:
  - NSSM service status (Running / Stopped / not-installed)
  - Most recent git commit touching the project's path
  - Newest log file timestamp (alarm if older than `log_stale_after_hours`)
  - Lock file presence (where applicable)
  - Aggregate status: green / yellow / red + one-liner reason

Cheap — runs synchronously per /api/projects/snapshot request. Each probe
is bounded with a short timeout so a hung service never blocks the page.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # .../live's parent
_LIVE_ROOT = Path(__file__).resolve().parent.parent.parent  # alias
_OPENDENTAL_REPO = Path(__file__).resolve().parent.parent.parent  # OpenDentalMCP repo root
# Note: __file__ is .../live/OpenDentalMCP/utilization_dashboard/projects_poller.py
# .parent.parent = .../live/OpenDentalMCP
# .parent.parent.parent = .../live
# We want git operations in the OpenDentalMCP repo root, which is .parent.parent
_GIT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ascend to repo root
# Re-derive cleanly: walk up until we find .git
def _find_git_root(start: Path) -> Path:
    cur = start
    for _ in range(8):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start


_GIT_ROOT = _find_git_root(Path(__file__).resolve())


@dataclass
class HealthCheck:
    service: Optional[str] = None
    service_status: Optional[str] = None    # "Running" | "Stopped" | None
    log_path: Optional[str] = None
    log_age_hours: Optional[float] = None
    log_stale_after_hours: int = 24
    log_stale: bool = False
    last_commit_age_days: Optional[float] = None
    last_commit_subject: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ProjectStatus:
    id: str
    name: str
    domain: str
    phase: str
    description: str
    repo_path: str
    health: HealthCheck
    overall: str          # "green" | "yellow" | "red" | "unknown"
    overall_reason: str
    notes_sources: list[str] = field(default_factory=list)


def load_registry(yaml_path: Optional[Path] = None) -> list[dict]:
    """Read projects.yaml. PyYAML is in the venv (used by other tools)."""
    yaml_path = yaml_path or (Path(__file__).resolve().parent / "projects.yaml")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        log.error("PyYAML not installed; install pyyaml in the venv")
        return []
    if not yaml_path.exists():
        return []
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return raw.get("projects", []) or []


# ---------------------------------------------------------------------------
# Service status — sc.exe query (no admin needed, fast)
# ---------------------------------------------------------------------------

def probe_service(service_name: Optional[str]) -> Optional[str]:
    if not service_name:
        return None
    try:
        proc = subprocess.run(
            ["sc.exe", "query", service_name],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("STATE"):
            # "STATE              : 4  RUNNING"
            parts = line.split()
            if len(parts) >= 4:
                return parts[3].title()  # "Running"
    return None


# ---------------------------------------------------------------------------
# Log freshness
# ---------------------------------------------------------------------------

def probe_log_age(log_path_rel: Optional[str]) -> Optional[float]:
    if not log_path_rel:
        return None
    p = _GIT_ROOT / log_path_rel
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    age_seconds = datetime.now().timestamp() - mtime
    return age_seconds / 3600.0


# ---------------------------------------------------------------------------
# Git activity per project path
# ---------------------------------------------------------------------------

def probe_git_last_commit(repo_path_rel: str) -> tuple[Optional[float], Optional[str]]:
    """Return (age_days, subject) for the most recent commit touching the
    project's repo_path. Operates from the git root."""
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%ct\t%s", "--", repo_path_rel],
            cwd=_GIT_ROOT,
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None
    line = proc.stdout.strip().splitlines()[0]
    try:
        ts_str, subject = line.split("\t", 1)
        commit_ts = float(ts_str)
    except (ValueError, IndexError):
        return None, None
    age_seconds = datetime.now().timestamp() - commit_ts
    return age_seconds / 86400.0, subject.strip()


# ---------------------------------------------------------------------------
# Aggregate status
# ---------------------------------------------------------------------------

def _aggregate(health: HealthCheck) -> tuple[str, str]:
    """Return (level, one-line reason) from the collected signals."""
    reasons: list[str] = []

    # Service: red if explicitly Stopped, yellow if expected but not present.
    if health.service:
        if health.service_status == "Running":
            pass
        elif health.service_status in ("Stopped", "Paused"):
            return "red", f"{health.service} is {health.service_status}"
        elif health.service_status is None:
            reasons.append(f"{health.service} not found")
        else:
            reasons.append(f"{health.service} is {health.service_status}")

    # Log staleness: yellow.
    if health.log_stale:
        reasons.append(f"log stale ({health.log_age_hours:.1f}h)"
                       if health.log_age_hours is not None
                       else "log not found")

    if reasons:
        return "yellow", "; ".join(reasons)

    # All checks pass and we got at least one positive signal.
    if health.service_status == "Running":
        return "green", "service running"
    if health.last_commit_age_days is not None and health.last_commit_age_days < 30:
        return "green", "recent dev activity"
    return "unknown", "no signals"


def status_for(project: dict) -> ProjectStatus:
    """Run all checks for one project entry and return a ProjectStatus."""
    h_cfg = project.get("health") or {}
    health = HealthCheck(
        service=h_cfg.get("service"),
        log_path=h_cfg.get("log_path"),
        log_stale_after_hours=int(h_cfg.get("log_stale_after_hours", 24)),
    )

    health.service_status = probe_service(health.service)
    health.log_age_hours = probe_log_age(health.log_path)
    if health.log_age_hours is not None:
        health.log_stale = health.log_age_hours > health.log_stale_after_hours

    age_days, subject = probe_git_last_commit(project["repo_path"])
    health.last_commit_age_days = age_days
    health.last_commit_subject = subject

    overall, reason = _aggregate(health)
    return ProjectStatus(
        id=project["id"],
        name=project["name"],
        domain=project.get("domain", ""),
        phase=project.get("phase", ""),
        description=(project.get("description") or "").strip(),
        repo_path=project["repo_path"],
        health=health,
        overall=overall,
        overall_reason=reason,
        notes_sources=project.get("notes_sources") or [],
    )


def status_all(yaml_path: Optional[Path] = None) -> list[dict]:
    """Run status_for() across the registry. Returns plain dicts for JSON."""
    out: list[dict] = []
    for entry in load_registry(yaml_path):
        try:
            ps = status_for(entry)
            out.append(asdict(ps))
        except Exception as e:  # never let one project break the page
            log.exception("project %s health probe failed: %s", entry.get("id"), e)
            out.append({
                "id": entry.get("id"),
                "name": entry.get("name"),
                "overall": "red",
                "overall_reason": f"probe error: {type(e).__name__}",
            })
    return out
