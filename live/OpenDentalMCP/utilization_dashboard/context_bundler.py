"""Build a context blob for "next steps" generation.

For one project, gather:
  - Repo files referenced in projects.yaml (README.md, PLAN.md, NEXT.md, etc.)
  - Memory files referenced in projects.yaml (project_*.md from
    ~/.claude/projects/<workspace>/memory/)
  - Last N git commits touching the project path
  - Last N lines of the configured log

Caps each section at a sensible token count so the prompt stays within Sonnet
limits without truncating critical context.

Returns (prompt: str, summary: dict) where summary is metadata about what
was included (sizes, file paths) — useful for the dashboard to show "this
recommendation was based on X commits, Y memory files, Z log lines."

Phase 2 enhancement (deferred): include search_session_transcripts results
via an MCP tool call from inside the agent loop. For v1 we stay file-bounded.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Path resolution mirrors projects_poller's logic.
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

# Memory dir is workspace-scoped. The CCD project key uses double-dashes for
# path separators. We hardcode the path the user is on; if it ever changes,
# the env var lets the operator override.
_DEFAULT_MEMORY_DIR = Path.home() / ".claude" / "projects" / \
    "C--Users-Administrator-Desktop-Cursor-OpenDentalMCP" / "memory"
MEMORY_DIR = Path(os.environ.get("UTILIZATION_MEMORY_DIR", str(_DEFAULT_MEMORY_DIR)))


# Section caps (chars, not tokens — rough)
MAX_DOC_CHARS = 4000
MAX_MEMORY_CHARS = 4000
MAX_LOG_LINES = 60
MAX_GIT_COMMITS = 8


@dataclass
class BundleSummary:
    repo_files_included: list[str] = field(default_factory=list)
    memory_files_included: list[str] = field(default_factory=list)
    git_commits_included: int = 0
    log_lines_included: int = 0
    total_chars: int = 0


def build(project: dict) -> tuple[str, BundleSummary]:
    """Build the prompt for `project` (a dict from projects.yaml).

    Returns (prompt_text, summary).
    """
    parts: list[str] = []
    summary = BundleSummary()

    parts.append(_section_header("Project metadata"))
    parts.append(_format_metadata(project))

    parts.append(_section_header("Recent git activity"))
    git_section, n_commits = _git_section(project["repo_path"])
    parts.append(git_section)
    summary.git_commits_included = n_commits

    parts.append(_section_header("Repo documentation"))
    docs_section, repo_files = _repo_files_section(project)
    parts.append(docs_section)
    summary.repo_files_included = repo_files

    parts.append(_section_header("Memory notes"))
    mem_section, mem_files = _memory_section(project)
    parts.append(mem_section)
    summary.memory_files_included = mem_files

    parts.append(_section_header("Recent log activity"))
    log_section, log_count = _log_section(project)
    parts.append(log_section)
    summary.log_lines_included = log_count

    body = "\n\n".join(parts)
    summary.total_chars = len(body)
    prompt = _wrap_with_instructions(project, body)
    return prompt, summary


def _section_header(title: str) -> str:
    return f"\n## {title}\n"


def _format_metadata(project: dict) -> str:
    desc = (project.get("description") or "").strip()
    return (
        f"- id: {project['id']}\n"
        f"- name: {project['name']}\n"
        f"- domain: {project.get('domain', '')}\n"
        f"- phase: {project.get('phase', '')}\n"
        f"- repo path: {project['repo_path']}\n"
        f"- description:\n  {desc.replace(chr(10), chr(10) + '  ')}"
    )


def _git_section(repo_path_rel: str) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            ["git", "log", f"-{MAX_GIT_COMMITS}",
             "--format=%h %ar  %s", "--", repo_path_rel],
            cwd=_GIT_ROOT,
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"(git log failed: {e})", 0
    if proc.returncode != 0:
        return f"(git log returned {proc.returncode})", 0
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return "(no commits touching this path)", 0
    return "\n".join("- " + ln for ln in lines), len(lines)


def _repo_files_section(project: dict) -> tuple[str, list[str]]:
    repo_path = _GIT_ROOT / project["repo_path"]
    sources = [s for s in (project.get("notes_sources") or [])
               if s.startswith("repo:")]
    chunks: list[str] = []
    included: list[str] = []
    remaining = MAX_DOC_CHARS
    for spec in sources:
        rel = spec.split(":", 1)[1]
        path = repo_path / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = text[: min(remaining, MAX_DOC_CHARS)]
        if not snippet.strip():
            continue
        chunks.append(f"### {rel}\n{snippet}")
        included.append(rel)
        remaining -= len(snippet)
        if remaining <= 200:
            break
    if not chunks:
        return "(no repo doc sources found)", []
    return "\n\n".join(chunks), included


def _memory_section(project: dict) -> tuple[str, list[str]]:
    sources = [s for s in (project.get("notes_sources") or [])
               if s.startswith("memory:")]
    chunks: list[str] = []
    included: list[str] = []
    remaining = MAX_MEMORY_CHARS
    for spec in sources:
        name = spec.split(":", 1)[1]
        # Allow the user to omit the .md suffix
        candidate = MEMORY_DIR / (name if name.endswith(".md") else name + ".md")
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = text[: min(remaining, MAX_MEMORY_CHARS)]
        if not snippet.strip():
            continue
        chunks.append(f"### memory/{candidate.name}\n{snippet}")
        included.append(candidate.name)
        remaining -= len(snippet)
        if remaining <= 200:
            break
    if not chunks:
        return "(no memory sources matched)", []
    return "\n\n".join(chunks), included


def _log_section(project: dict) -> tuple[str, int]:
    h = project.get("health") or {}
    log_path_rel = h.get("log_path")
    if not log_path_rel:
        return "(no log configured)", 0
    p = _GIT_ROOT / log_path_rel
    if not p.exists():
        return f"(log not found: {log_path_rel})", 0
    try:
        # Read last MAX_LOG_LINES * ~200 chars to avoid loading huge logs
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            tail_size = min(size, MAX_LOG_LINES * 400)
            f.seek(size - tail_size)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return f"(log read failed: {e})", 0
    lines = tail.splitlines()[-MAX_LOG_LINES:]
    if not lines:
        return "(log empty)", 0
    return "\n".join(lines), len(lines)


_INSTRUCTIONS = """\
You are a project advisor reviewing the current state of one software project
in a multi-project monorepo. The developer wants concrete, opinionated next
steps — not a status summary.

Read all the context below, then produce 2 to 4 next-step bullets. Each
bullet must:
  - Begin with an imperative verb (Add / Fix / Investigate / Migrate / Test /
    Document / Refactor / Stop / Verify)
  - Be specific to THIS project's current state (cite a file, a recent
    commit, a stale log line, or a memory note when relevant)
  - Be doable in under 2 hours of focused work
  - Reflect actual signal in the context — if the project looks healthy and
    idle, say so and suggest a small improvement rather than inventing work

Output format:
  - Markdown bullets only. No preamble. No closing summary. No links.
  - 2-4 bullets total. Quality over quantity.
  - If the context is genuinely too thin to recommend anything, output a
    single bullet that names exactly what's missing (e.g. "Document the
    project's purpose in README.md — current README is empty.")

Begin context.

"""


def _wrap_with_instructions(project: dict, body: str) -> str:
    return _INSTRUCTIONS + body
