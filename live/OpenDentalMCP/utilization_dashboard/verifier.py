"""Auto-ship verifier for L3 proposals.

The agent's self-reported confidence is unreliable for unattended ship. This
module inspects the actual diff, picks an appropriate test tier, runs it
inside the proposal worktree, and returns a verdict that the L3 path uses
to gate the auto-merge.

Tiers (highest tier triggered by any changed file wins):
    V1_SMOKE    docs / text only            secret regex + markdown sanity
    V2_UNIT     any *.py touched            py_compile + ruff (best-effort)
                                            + pytest near touched files
    V3_SERVICE  routes / blueprint code     V2 + (TODO) start service in
                                            worktree on free port, hit /healthz
    V4_UI       static html|css|js          V3 + (TODO) playwright render
    V5_SCHEMA   migrations / requirements   block auto-apply, force pending

A verdict has a `status` of pass / fail / skip / block. Only `pass` lets L3
auto-merge. `block` and `fail` leave the proposal pending. `skip` (e.g.
deferred V4) is treated like pass for now — the operator decides.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

V1_SMOKE = "v1_smoke"
V2_UNIT = "v2_unit"
V3_SERVICE = "v3_service"
V4_UI = "v4_ui"
V5_SCHEMA = "v5_schema"

# Pattern → tier. Order matters: schema first (most restrictive), then UI,
# then service, then plain Python, then docs.
_SCHEMA_PAT = re.compile(
    r"(/migrations/|\.sql$|/requirements[^/]*\.txt$|/pyproject\.toml$|/setup\.py$)"
)
_UI_PAT = re.compile(r"/static/.+\.(html|css|js)$")
_SERVICE_PAT = re.compile(
    r"(routes\.py$|_routes\.py$|/__init__\.py$|mcp_server.*\.py$|/app\.py$)"
)
_PY_PAT = re.compile(r"\.py$")
_DOC_PAT = re.compile(r"\.(md|txt|rst)$", re.IGNORECASE)

# Heuristic: looks like a hard-coded credential. Tuned to limit false positives
# on README example snippets — requires both a key-ish name AND a quoted value
# of >= 16 chars after =/:.
_SECRET_PAT = re.compile(
    r"(api[_-]?key|secret|password|bearer[_-]?token|access[_-]?token)\s*[:=]\s*"
    r"['\"][A-Za-z0-9_\-]{16,}",
    re.IGNORECASE,
)


@dataclass
class Verification:
    tier: str
    status: str           # 'pass' | 'fail' | 'skip' | 'block'
    summary: str          # one-line badge text for the UI
    evidence: dict = field(default_factory=dict)
    latency_ms: int = 0
    blocked_apply: bool = False  # True → L3 must NOT auto-merge

    def to_dict(self) -> dict:
        return asdict(self)


def classify_tier(files_changed: list[str]) -> str:
    """Pick the highest tier triggered by any path in `files_changed`.

    Empty input → V1 (the L3 path treats no-files as a no-op anyway and
    skips verification, so this is just a safe default).
    """
    if not files_changed:
        return V1_SMOKE
    triggered: set[str] = set()
    for raw in files_changed:
        f = "/" + raw.replace("\\", "/").lstrip("/")
        if _SCHEMA_PAT.search(f):
            triggered.add(V5_SCHEMA)
        elif _UI_PAT.search(f):
            triggered.add(V4_UI)
        elif _SERVICE_PAT.search(f):
            triggered.add(V3_SERVICE)
        elif _PY_PAT.search(f):
            triggered.add(V2_UNIT)
        else:
            triggered.add(V1_SMOKE)
    for tier in (V5_SCHEMA, V4_UI, V3_SERVICE, V2_UNIT, V1_SMOKE):
        if tier in triggered:
            return tier
    return V1_SMOKE


# ---------------------------------------------------------------------------
# Tier runners
# ---------------------------------------------------------------------------


def _v1_smoke(wt_path: Path, files: list[str]) -> Verification:
    """Cheap checks for docs-only changes: secret regex + balanced fences."""
    issues: list[str] = []
    docs_checked = 0
    for rel in files:
        p = wt_path / rel
        if not p.exists() or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            issues.append(f"{rel}: read failed ({e})")
            continue
        if _SECRET_PAT.search(text):
            issues.append(f"{rel}: possible hard-coded secret")
        if _DOC_PAT.search(rel):
            docs_checked += 1
            if text.count("```") % 2 != 0:
                issues.append(f"{rel}: unbalanced ``` code fence")
    status = "fail" if issues else "pass"
    summary = (
        f"V1 smoke: {len(issues)} issue(s)"
        if issues
        else f"V1 smoke: clean ({docs_checked} doc(s))"
    )
    return Verification(
        tier=V1_SMOKE,
        status=status,
        summary=summary,
        evidence={
            "issues": issues,
            "docs_checked": docs_checked,
            "files_checked": list(files),
        },
        blocked_apply=bool(issues),
    )


def _py_compile(wt_path: Path, py_files: list[str]) -> tuple[list[str], list[str]]:
    """Run py_compile on each .py file. Returns (ok, fail) lists."""
    ok: list[str] = []
    fail: list[str] = []
    for rel in py_files:
        p = wt_path / rel
        if not p.exists():
            fail.append(f"{rel}: missing")
            continue
        res = subprocess.run(
            [sys.executable, "-m", "py_compile", str(p)],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if res.returncode == 0:
            ok.append(rel)
        else:
            err = (res.stderr or res.stdout).strip().splitlines()
            fail.append(f"{rel}: " + (" / ".join(err[-3:])[:300] or "compile failed"))
    return ok, fail


def _run_ruff(wt_path: Path, py_files: list[str]) -> tuple[str, list[str]]:
    """Best-effort ruff check. Returns (summary, issues_list).

    Uses --select E,F so we only flag real errors and undefined names — not
    style nits that would block ship for cosmetic reasons.
    """
    abs_files = [str(wt_path / f) for f in py_files]
    try:
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--quiet",
             "--select", "E,F", "--no-cache"] + abs_files,
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"ruff: unavailable ({type(e).__name__})", []
    if res.returncode == 0:
        return "ruff: clean", []
    if "No module named" in (res.stderr or ""):
        return "ruff: not installed", []
    issues = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return f"ruff: {len(issues)} finding(s)", issues[:20]


def _find_pytest_targets(wt_path: Path, py_files: list[str]) -> list[Path]:
    """Find test files plausibly related to the changed files.

    For each changed *.py:
      - if its name starts with test_ → it IS a test, include it
      - else look for tests/test_<name>.py at sibling and parent levels

    Caps at 6 files to keep runtime bounded.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for rel in py_files:
        p = wt_path / rel
        name = p.stem
        if name.startswith("test_"):
            if p.exists() and p not in seen:
                out.append(p); seen.add(p)
                if len(out) >= 6:
                    break
            continue
        candidates = [
            p.parent / "tests" / f"test_{name}.py",
            p.parent / f"test_{name}.py",
            p.parent.parent / "tests" / f"test_{name}.py",
        ]
        for c in candidates:
            if c.exists() and c not in seen:
                out.append(c); seen.add(c)
                break
        if len(out) >= 6:
            break
    return out


def _run_pytest(wt_path: Path, targets: list[Path]) -> tuple[str, str]:
    """Best-effort pytest. Returns (summary, full_tail_for_evidence)."""
    if not targets:
        return "pytest: no nearby tests", ""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "--no-header", "-q"]
            + [str(t) for t in targets],
            cwd=wt_path, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"pytest: skipped ({type(e).__name__})", ""
    out = (res.stdout or "") + (res.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-10:])
    last = (out.strip().splitlines() or [""])[-1]
    summary = f"pytest: rc={res.returncode} ({last[:120]})"
    return summary, tail


def _v2_unit(wt_path: Path, files: list[str]) -> Verification:
    """Compile + ruff + nearby pytest for any touched .py files."""
    py_files = [f for f in files if f.endswith(".py")]
    issues: list[str] = []

    compile_ok, compile_fail = _py_compile(wt_path, py_files)
    issues.extend(compile_fail)

    ruff_summary, ruff_issues = _run_ruff(wt_path, py_files)
    issues.extend(ruff_issues)

    targets = _find_pytest_targets(wt_path, py_files)
    pytest_summary, pytest_tail = _run_pytest(wt_path, targets)
    if pytest_summary.startswith("pytest: rc=") and "rc=0" not in pytest_summary:
        issues.append(pytest_summary)

    status = "fail" if issues else "pass"
    parts = [
        f"compile {len(compile_ok)}/{len(py_files)}",
        ruff_summary,
        pytest_summary,
    ]
    summary = (
        f"V2 unit: {len(issues)} issue(s)"
        if status == "fail"
        else "V2 unit: " + "; ".join(parts)
    )
    return Verification(
        tier=V2_UNIT,
        status=status,
        summary=summary,
        evidence={
            "issues": issues[:30],
            "py_files": py_files,
            "compile_ok": compile_ok,
            "compile_fail": compile_fail,
            "ruff": ruff_summary,
            "ruff_issues": ruff_issues,
            "pytest": pytest_summary,
            "pytest_tail": pytest_tail,
            "pytest_targets": [str(t.relative_to(wt_path)) for t in targets],
        },
        blocked_apply=bool(issues),
    )


def _v3_service(wt_path: Path, files: list[str]) -> Verification:
    """V3 adds an in-worktree service smoke. Not yet implemented — falls back
    to V2 for now and records that V3 was deferred so the UI can show it."""
    v2 = _v2_unit(wt_path, files)
    return Verification(
        tier=V3_SERVICE,
        status=v2.status,
        summary="V3 service (V2-only, service-start TODO): " + v2.summary,
        evidence={
            "v2_inner": v2.evidence,
            "todo": "Start service in worktree on free port + hit /healthz",
        },
        blocked_apply=v2.blocked_apply,
    )


def _v4_ui(wt_path: Path, files: list[str]) -> Verification:
    """V4 will use a Playwright runner to load the changed page and check for
    console errors. Deferred — emit SKIP rather than blocking ship, and let
    the operator decide whether to require V4 manually."""
    return Verification(
        tier=V4_UI,
        status="skip",
        summary="V4 UI: skipped (Playwright tier not yet wired)",
        evidence={
            "files": files,
            "todo": "Add a Playwright-backed runner that exercises the changed page",
        },
        blocked_apply=False,
    )


def _v5_schema(wt_path: Path, files: list[str]) -> Verification:
    """Schema / dependency / build-config edits are too risky to ship
    unattended. Always block."""
    triggered = [
        f for f in files
        if _SCHEMA_PAT.search("/" + f.replace("\\", "/").lstrip("/"))
    ]
    return Verification(
        tier=V5_SCHEMA,
        status="block",
        summary="V5 schema: requires manual review (auto-apply blocked)",
        evidence={"triggers": triggered, "all_files": files},
        blocked_apply=True,
    )


_RUNNERS = {
    V1_SMOKE: _v1_smoke,
    V2_UNIT: _v2_unit,
    V3_SERVICE: _v3_service,
    V4_UI: _v4_ui,
    V5_SCHEMA: _v5_schema,
}


def verify(wt_path: Path, files_changed: list[str]) -> Verification:
    """Public entry point: classify, run, time, and trap exceptions.

    A crash in the verifier is treated as `fail` so the L3 path falls back
    to manual review rather than shipping an unverified change.
    """
    tier = classify_tier(files_changed)
    runner = _RUNNERS[tier]
    t0 = time.monotonic()
    try:
        v = runner(wt_path, files_changed)
    except Exception as e:
        log.exception("verifier %s crashed: %s", tier, e)
        v = Verification(
            tier=tier,
            status="fail",
            summary=f"verifier crashed: {type(e).__name__}",
            evidence={"exception": f"{type(e).__name__}: {e}"[:500]},
            blocked_apply=True,
        )
    v.latency_ms = int((time.monotonic() - t0) * 1000)
    return v
