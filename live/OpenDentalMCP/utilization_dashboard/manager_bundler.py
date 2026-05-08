"""Bundle all-project state into one prompt for the manager agent.

The per-project bundler (context_bundler.py) builds context for a single
project's tactical work. This one builds context for portfolio-level
strategic thinking: how do the projects interact, what patterns emerge,
what's missing across the whole.

Inputs:
  - Per-project status (health, last commit, log freshness)
  - Per-project memory notes (whatever the registry pointed at)
  - Per-project latest next-steps (the bullets the user already has)
  - Per-project latest agent investigations (when the agent dug deeper)
  - Per-project latest proposals (what the agent drafted)

Output:
  Markdown text wrapped with manager-specific instructions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utilization_dashboard import context_bundler, projects_poller, projects_storage

log = logging.getLogger(__name__)

# Per-project sections are capped tighter than the single-project bundler
# so we can fit ~6 projects in one Sonnet call without truncation.
MAX_MEMORY_PER_PROJECT_CHARS = 1800
MAX_GIT_COMMITS_PER_PROJECT = 4
MAX_NEXT_STEPS_CHARS = 1200
MAX_INVESTIGATIONS_PER_PROJECT = 1
MAX_PROPOSALS_PER_PROJECT = 1


@dataclass
class ManagerBundleSummary:
    project_ids: list[str] = field(default_factory=list)
    total_chars: int = 0
    sections_per_project: dict[str, list[str]] = field(default_factory=dict)


def build() -> tuple[str, ManagerBundleSummary]:
    """Build the manager prompt. Returns (prompt_text, summary)."""
    registry = projects_poller.load_registry()
    summary = ManagerBundleSummary()

    parts: list[str] = [_INSTRUCTIONS, _BEGIN_CONTEXT]

    # Add a portfolio-level overview line so the manager has the roster up
    # front before drilling into per-project sections.
    parts.append(_section_header("Portfolio roster"))
    roster_lines = []
    for entry in registry:
        roster_lines.append(
            f"- **{entry.get('name')}** ({entry.get('id')}) — domain: "
            f"{entry.get('domain', '?')}, phase: {entry.get('phase', '?')}"
        )
    parts.append("\n".join(roster_lines))

    for entry in registry:
        project_sections: list[str] = []
        pid = entry["id"]
        status = projects_poller.status_for(entry)
        next_steps = projects_storage.latest(pid)
        investigations = projects_storage.investigations_for_project(pid)
        proposals = projects_storage.proposals_for_project(pid)

        parts.append(_section_header(f"Project: {entry['name']} ({pid})"))
        parts.append(_format_meta(entry, status))
        project_sections.append("metadata")

        git = _git_section(entry["repo_path"])
        if git:
            parts.append("**Recent commits:**\n" + git)
            project_sections.append("git")

        mem = _memory_section(entry)
        if mem:
            parts.append("**Memory notes:**\n" + mem)
            project_sections.append("memory")

        if next_steps and next_steps.get("content"):
            cap = next_steps["content"][:MAX_NEXT_STEPS_CHARS]
            parts.append("**Latest tactical next-steps (already generated):**\n" + cap)
            project_sections.append("next_steps")

        # One representative investigation if any exists
        if investigations:
            inv = next(iter(investigations.values()))
            inv_text = (inv.get("content") or "")[:1000]
            if inv_text:
                parts.append(f"**Recent investigation finding:**\n{inv_text}")
                project_sections.append("investigation")

        if proposals:
            prop = next(iter(proposals.values()))
            prop_summary = (prop.get("summary") or "")[:600]
            files = prop.get("files_changed") or []
            if prop_summary:
                files_str = ", ".join(files[:5]) if files else "no files"
                parts.append(
                    f"**Recent agent proposal** (status: {prop.get('status')}):\n"
                    f"{prop_summary}\n"
                    f"Files: {files_str}"
                )
                project_sections.append("proposal")

        summary.project_ids.append(pid)
        summary.sections_per_project[pid] = project_sections

    body = "\n\n".join(parts)
    summary.total_chars = len(body)
    return body, summary


def _section_header(title: str) -> str:
    return f"\n## {title}\n"


def _format_meta(entry: dict, status) -> str:
    health = status.health
    return (
        f"- description: {(entry.get('description') or '').strip()[:400]}\n"
        f"- status: **{status.overall}** — {status.overall_reason}\n"
        f"- service: {health.service or '—'} ({health.service_status or '—'})\n"
        f"- last commit age: "
        f"{health.last_commit_age_days:.1f}d" if health.last_commit_age_days is not None else "—"
    )


def _git_section(repo_path_rel: str) -> str:
    import subprocess
    git_root = projects_poller._GIT_ROOT
    try:
        proc = subprocess.run(
            ["git", "log", f"-{MAX_GIT_COMMITS_PER_PROJECT}",
             "--format=%h %ar  %s", "--", repo_path_rel],
            cwd=git_root,
            capture_output=True, text=True, timeout=4,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    lines = proc.stdout.strip().splitlines()
    return "\n".join("- " + ln for ln in lines)


def _memory_section(entry: dict) -> str:
    sources = [s for s in (entry.get("notes_sources") or [])
               if s.startswith("memory:")]
    chunks: list[str] = []
    remaining = MAX_MEMORY_PER_PROJECT_CHARS
    for spec in sources:
        name = spec.split(":", 1)[1]
        candidate = context_bundler.MEMORY_DIR / (name if name.endswith(".md") else name + ".md")
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = text[: min(remaining, MAX_MEMORY_PER_PROJECT_CHARS)]
        if not snippet.strip():
            continue
        chunks.append(f"- *{candidate.name}*: {snippet.strip()[:400]}…")
        remaining -= len(snippet)
        if remaining <= 200:
            break
    return "\n".join(chunks)


def build_action_prompt(action: str, bullet_text: str, section: str = "") -> tuple[str, ManagerBundleSummary]:
    """Build a prompt for one of the per-bullet manager actions.

    `action` is one of:
      - 'plan'         -> produce an implementation plan for this bullet
      - 'investigate'  -> validate the manager's evidence for this bullet
    """
    body, summary = build()
    # Strip the manager-level instructions block; we want the raw context only.
    body = body.replace(_INSTRUCTIONS, "", 1)

    instructions = _ACTION_INSTRUCTIONS.get(action)
    if not instructions:
        raise ValueError(f"unknown manager action: {action!r}")

    section_hint = f"(from section: {section})" if section else ""
    prompt = (
        instructions
        + f"\nThe specific bullet to act on {section_hint}:\n  > {bullet_text}\n\n"
        + body
    )
    return prompt, summary


def build_user_idea_prompt(idea: str) -> tuple[str, ManagerBundleSummary]:
    """Build a prompt that asks Opus to turn a free-form user idea into a
    single polished bullet for the manager brief, with section assignment.
    """
    body, summary = build()
    body = body.replace(_INSTRUCTIONS, "", 1)
    prompt = (
        _USER_IDEA_INSTRUCTIONS
        + f"\nUser's idea (verbatim):\n  > {idea.strip()}\n\n"
        + body
    )
    return prompt, summary


_USER_IDEA_INSTRUCTIONS = """\
The user has submitted an idea about the multi-project portfolio below.
Your job is to turn that idea into ONE polished bullet for the manager
brief, written in the same voice as the brief itself, with proper section
assignment.

Read the user's idea carefully. Read the project context. Then output
EXACTLY this format (markdown, nothing else — no preamble, no closing):

### <SECTION>
- **<short title in bold>** — <one to three sentences expanding the idea
  with concrete reference to which projects this affects, why it matters
  given the current state, and what specifically should happen. If the
  idea is already addressed by something visible in the context, say so
  and cite it.>

Where <SECTION> must be EXACTLY one of:
  - Cross-project opportunities
  - Patterns
  - Strategic recommendations
  - User-submitted ideas

Choose the section that best fits:
  - "Cross-project opportunities" if the idea connects two or more
    projects or proposes integration between them
  - "Patterns" if the idea is an observation about something repeated
    across the portfolio
  - "Strategic recommendations" if the idea is a new piece of work or
    architectural move
  - "User-submitted ideas" only if none of the above fit cleanly

If the user's idea is unclear, off-topic for this portfolio, or already
addressed by something in the context, still produce a bullet — but make
the bullet's prose explicitly note that, e.g.: "**Already covered** —
this overlaps with the existing 'Unify NSSM logs' opportunity above..."

Do NOT produce more than one bullet. Do NOT add commentary outside the
bullet. Output starts with "### " and ends with the bullet text.
"""


_ACTION_INSTRUCTIONS = {
    "plan": """\
You are a software portfolio architect. The portfolio's manager just made a
strategic recommendation about the multi-project codebase below. Your job is
to turn that recommendation into a concrete implementation plan.

Read all the project context, then produce a markdown plan in EXACTLY this
shape (no preamble, no closing summary):

### Affected projects
- Bullet per project this touches, with one line on what changes there

### Implementation steps
- 3 to 7 sequential bullets. Each is a discrete chunk of work an engineer
  could complete in a single sitting. Cite files / projects by name.

### Acceptance criteria
- 1 to 3 bullets describing exactly how we'd know the work is done

### Risks
- 1 to 2 bullets — what could go wrong, what to watch for

### Effort
- T-shirt size (S / M / L / XL) and rough hour estimate, with a one-line
  justification

If the recommendation is a no-op or already implemented, say so explicitly
in a single sentence rather than inventing work.
""",
    "investigate": """\
You are validating a manager's strategic recommendation against the actual
state of the codebase. The manager's claim might be right, partly right, or
based on stale data. Your job is to check the evidence.

Use the read-only tools (Read, Grep, Glob) on the project context below to
verify the specific facts the manager cited. Then write a short report:

### Evidence found
- 2 to 4 bullets of CONCRETE evidence (cite file paths, line numbers,
  commits, log snippets) that either support or contradict the claim

### Assessment
- One bullet: SUPPORTED | PARTIALLY_SUPPORTED | CONTRADICTED | INSUFFICIENT_DATA
  with one line of reasoning

### Recommendation
- If SUPPORTED: 1 to 2 next steps to act on it
- If CONTRADICTED: explain what the actual situation is
- If PARTIAL or INSUFFICIENT: what additional evidence would clarify

Be concise. No preamble. Cite specific evidence — do not assert without
naming files or commits.
""",
}


_INSTRUCTIONS = """\
You are a software portfolio manager reviewing six related software projects
that all serve a single small business (a dental practice's MCP server fleet).
Your job is NOT to recommend per-project bug fixes — those are handled by
per-project tactical agents. Your job is to think across the whole portfolio.

Read all the project context below, then produce a brief in this format
(markdown only, no preamble, no closing summary):

### Cross-project opportunities
2 to 4 bullets identifying concrete places where two or more projects
could integrate, share code, sequence, or eliminate duplication. Cite
specific projects by name. Be concrete, not generic.

### Patterns
1 to 3 short observations about themes you notice across the portfolio
(e.g. "three projects log to <name> but with inconsistent schemas").
Cite specific evidence.

### Strategic recommendations
2 to 4 bullets of NEW work — not tactical fixes — to consider. Each
should explain *why now* given current state. Format each as:
  - **<one-sentence proposal>** — why this is the right next move

If the portfolio looks healthy and there's nothing strategic to recommend,
say so explicitly in one sentence rather than inventing work.

"""

_BEGIN_CONTEXT = "\n--- BEGIN PORTFOLIO CONTEXT ---\n"
