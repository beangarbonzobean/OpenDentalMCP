"""Skill launcher backend.

Reads `skills.yaml`, exposes the curated skill list, and runs a chosen skill
by spawning `claude -p "<prompt>" --output-format stream-json` in a worker
thread.

Why subprocess and not the SDK? The video's UX is "click → terminal-equivalent
output" — we want the same Claude Code session semantics (skills auto-loaded
via the system prompt's available-skills block, plugin skills resolved, etc.)
that you'd get from typing the prompt at a terminal. The SDK provider in
inference_router is tuned for tool-restricted internal dispatches; for skill
launches we want the full session, so we shell out to the same `claude.exe`
the SDK provider already locates.

Each run is logged to a `skill_run` row in utilization.db with status
running -> ok|failed, plus stdout / stderr / final assistant message text.
The frontend polls /api/skills/runs/<id> every 2s until status != running.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

import yaml

from utilization_dashboard import async_runner

log = logging.getLogger(__name__)


SKILLS_YAML = Path(os.environ.get(
    "UTILIZATION_SKILLS_YAML",
    Path(__file__).resolve().parent / "skills.yaml",
))

DB_PATH = Path(os.environ.get(
    "UTILIZATION_DB_PATH",
    Path(__file__).resolve().parent / "data" / "utilization.db",
))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_run (
    id           TEXT PRIMARY KEY,            -- uuid4
    started_ts   TEXT NOT NULL,               -- ISO UTC
    finished_ts  TEXT,                        -- ISO UTC, null while running
    skill_id     TEXT NOT NULL,
    skill_name   TEXT NOT NULL,
    input_text   TEXT,
    prompt       TEXT NOT NULL,               -- rendered prompt sent to claude
    cwd          TEXT,
    status       TEXT NOT NULL,               -- running | ok | failed | timeout
    output_text  TEXT,                        -- final assistant message text
    stderr_text  TEXT,                        -- subprocess stderr (truncated)
    exit_code    INTEGER,
    latency_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_skill_run_started ON skill_run(started_ts DESC);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(_SCHEMA)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Skills config
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    id: str
    name: str
    domain: str
    description: str
    prompt_template: str
    requires_input: bool
    input_label: str
    cwd: Optional[str]
    timeout_sec: int
    model: Optional[str]

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "description": self.description,
            "requires_input": self.requires_input,
            "input_label": self.input_label,
            "has_input_placeholder": "{input}" in self.prompt_template,
        }


def load_skills() -> list[Skill]:
    if not SKILLS_YAML.exists():
        return []
    with open(SKILLS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    skills = []
    for raw in data.get("skills", []) or []:
        try:
            skills.append(Skill(
                id=raw["id"],
                name=raw["name"],
                domain=raw.get("domain", "General"),
                description=raw.get("description", ""),
                prompt_template=raw["prompt_template"],
                requires_input=bool(raw.get("requires_input", False)),
                input_label=raw.get("input_label", ""),
                cwd=raw.get("cwd"),
                timeout_sec=int(raw.get("timeout_sec", 600)),
                model=raw.get("model"),
            ))
        except KeyError as e:
            log.warning("skills.yaml: skipping entry missing field %s", e)
    return skills


def find_skill(skill_id: str) -> Optional[Skill]:
    for s in load_skills():
        if s.id == skill_id:
            return s
    return None


# ---------------------------------------------------------------------------
# Claude CLI discovery (mirrors inference_router/providers/claude_agent_sdk_provider.py)
# ---------------------------------------------------------------------------

def find_claude_cli() -> Optional[str]:
    override = os.environ.get("CLAUDE_AGENT_CLI_PATH", "")
    if override and Path(override).exists():
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    win_roots = [
        os.path.expandvars(r"%APPDATA%\Claude\claude-code"),
        os.path.expanduser(r"~\AppData\Roaming\Claude\claude-code"),
    ]
    candidates: list[str] = []
    for root in win_roots:
        if os.path.isdir(root):
            candidates.extend(glob.glob(os.path.join(root, "*", "claude.exe")))
    if not candidates:
        return None
    return max(candidates)


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

def start_run(skill_id: str, user_input: str) -> dict:
    """Render the prompt, write a 'running' row, dispatch to thread pool.
    Returns {run_id, status, started_ts}."""
    skill = find_skill(skill_id)
    if skill is None:
        raise ValueError(f"unknown skill: {skill_id}")

    if skill.requires_input and not user_input.strip():
        raise ValueError(f"skill '{skill_id}' requires input")

    prompt = skill.prompt_template.replace("{input}", user_input.strip())

    run_id = uuid4().hex
    started_ts = _now_iso()
    init_db()
    with _conn() as db:
        db.execute(
            "INSERT INTO skill_run "
            "(id, started_ts, skill_id, skill_name, input_text, prompt, cwd, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
            (run_id, started_ts, skill.id, skill.name, user_input, prompt, skill.cwd),
        )

    async_runner.submit(_execute_run, run_id, skill, prompt)
    return {"run_id": run_id, "status": "running", "started_ts": started_ts}


def _execute_run(run_id: str, skill: Skill, prompt: str) -> None:
    """Worker: spawn claude -p, capture output, update the row."""
    cli = find_claude_cli()
    if cli is None:
        _finalize(
            run_id, status="failed", output="", stderr="claude CLI not found",
            exit_code=-1, latency_ms=0,
        )
        return

    cmd = [cli, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if skill.model:
        cmd.extend(["--model", skill.model])

    cwd = skill.cwd if skill.cwd and Path(skill.cwd).is_dir() else None

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=skill.timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        latency = int((time.monotonic() - started) * 1000)
        _finalize(
            run_id, status="timeout",
            output=_extract_text(getattr(e, "stdout", "") or ""),
            stderr=(getattr(e, "stderr", "") or "")[-4000:],
            exit_code=-1, latency_ms=latency,
        )
        return
    except Exception as e:  # noqa: BLE001
        latency = int((time.monotonic() - started) * 1000)
        log.exception("skill subprocess failed: %s", e)
        _finalize(
            run_id, status="failed", output="",
            stderr=f"{type(e).__name__}: {e}",
            exit_code=-1, latency_ms=latency,
        )
        return

    latency = int((time.monotonic() - started) * 1000)
    output = _extract_text(proc.stdout or "")
    stderr_tail = (proc.stderr or "")[-4000:]
    status = "ok" if proc.returncode == 0 else "failed"
    _finalize(
        run_id, status=status, output=output, stderr=stderr_tail,
        exit_code=proc.returncode, latency_ms=latency,
    )


def _extract_text(stream_json_stdout: str) -> str:
    """Parse `claude -p --output-format stream-json` output and concatenate
    the assistant text. Each line is a JSON event. We collect 'text' from
    every assistant message; the final 'result' event also carries the full
    text (we prefer that if present, fall back to concatenation)."""
    final_result = None
    chunks: list[str] = []
    for line in stream_json_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        ev_type = evt.get("type")
        if ev_type == "result" and isinstance(evt.get("result"), str):
            final_result = evt["result"]
        elif ev_type == "assistant":
            msg = evt.get("message", {})
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if text:
                        chunks.append(text)
    if final_result:
        return final_result
    return "\n".join(chunks).strip()


def _finalize(
    run_id: str,
    *,
    status: str,
    output: str,
    stderr: str,
    exit_code: int,
    latency_ms: int,
) -> None:
    with _conn() as db:
        db.execute(
            "UPDATE skill_run SET finished_ts=?, status=?, output_text=?, "
            "stderr_text=?, exit_code=?, latency_ms=? WHERE id=?",
            (_now_iso(), status, output, stderr, exit_code, latency_ms, run_id),
        )


# ---------------------------------------------------------------------------
# Read APIs for the frontend
# ---------------------------------------------------------------------------

def get_run(run_id: str) -> Optional[dict]:
    init_db()
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM skill_run WHERE id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 25) -> list[dict]:
    init_db()
    with _conn() as db:
        rows = db.execute(
            "SELECT id, started_ts, finished_ts, skill_id, skill_name, "
            "input_text, status, latency_ms, exit_code "
            "FROM skill_run ORDER BY started_ts DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(r) for r in rows]
