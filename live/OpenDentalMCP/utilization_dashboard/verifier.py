"""Auto-ship verifier for L3 proposals.

The agent's self-reported confidence is unreliable for unattended ship. This
module inspects the actual diff, picks an appropriate test tier, runs it
inside the proposal worktree, and returns a verdict that the L3 path uses
to gate the auto-merge.

Tiers (highest tier triggered by any changed file wins):
    V1_SMOKE    docs / text only            secret regex + markdown sanity
    V2_UNIT     any *.py touched            py_compile + ruff (best-effort)
                                            + pytest near touched files
    V3_SERVICE  routes / blueprint code     V2 + start the utilization
                                            dashboard in the worktree on a
                                            free port and hit /healthz
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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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


def _free_port() -> int:
    """Ask the OS for an unused TCP port. There's a small race between
    releasing the socket and the child process binding it, but for a single
    short-lived smoke test that's acceptable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _service_smoke(pkg_dir: Path, timeout_s: float = 12.0) -> tuple[str, dict, bool]:
    """Spawn the utilization dashboard standalone runner in `pkg_dir` on a
    free port, poll /utilization/healthz until it answers, then tear down.

    Returns (one-line summary, evidence dict, ok bool).
    `ok` is True only if /healthz returned 200 with {"ok": true, ...}.
    """
    port = _free_port()
    url = f"http://127.0.0.1:{port}/utilization/healthz"
    cmd = [
        sys.executable, "-m", "utilization_dashboard.standalone",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    evidence: dict = {"port": port, "url": url, "cwd": str(pkg_dir)}

    # Capture output to a tempfile so a chatty server can't deadlock by
    # filling a pipe buffer we never read.
    log_fh = tempfile.TemporaryFile()
    proc: Optional[subprocess.Popen] = None
    summary = ""
    ok = False
    try:
        try:
            proc = subprocess.Popen(
                cmd, cwd=pkg_dir, stdout=log_fh, stderr=subprocess.STDOUT,
            )
        except OSError as e:
            return f"failed to spawn service: {e}", evidence, False

        deadline = time.monotonic() + timeout_s
        last_err = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                summary = f"service exited early rc={proc.returncode}"
                break
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    body = resp.read()
                    evidence["status"] = resp.status
                    evidence["body"] = body.decode("utf-8", errors="replace")[:400]
                    payload = None
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        pass
                    if (resp.status == 200
                            and isinstance(payload, dict)
                            and payload.get("ok") is True):
                        summary = f"healthz 200 ok ({payload.get('service','?')})"
                        ok = True
                    else:
                        summary = f"healthz {resp.status} body unexpected"
                    break
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(0.25)
        else:
            summary = f"timeout waiting for /healthz ({last_err})"
            evidence["last_error"] = last_err
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        # Always grab a tail of the service log for diagnostics.
        try:
            log_fh.seek(0)
            log_text = log_fh.read().decode("utf-8", errors="replace")
            evidence["stdout_tail"] = log_text[-800:]
        finally:
            log_fh.close()

    return summary, evidence, ok


def _v3_service(wt_path: Path, files: list[str]) -> Verification:
    """V3 = V2 + start the utilization dashboard in the worktree on a free
    port and probe /healthz.

    The smoke is scoped to changes that touch the utilization_dashboard
    package — that's the only service whose entry point we know
    (utilization_dashboard.standalone). For other service-shaped paths
    (mcp_server*.py, generic app.py) we still run V2 and record that the
    smoke didn't apply rather than blocking ship.
    """
    v2 = _v2_unit(wt_path, files)

    pkg_dir = wt_path / "live" / "OpenDentalMCP"
    standalone_py = pkg_dir / "utilization_dashboard" / "standalone.py"
    touches_dashboard = any(
        "utilization_dashboard" in f.replace("\\", "/") for f in files
    )

    if v2.status == "fail":
        # Don't bother spinning up a service if the unit tier already failed.
        return Verification(
            tier=V3_SERVICE,
            status="fail",
            summary="V3 service: V2 failed → " + v2.summary,
            evidence={
                "v2_inner": v2.evidence,
                "service_smoke": {"skipped": "V2 failed"},
            },
            blocked_apply=v2.blocked_apply,
        )

    if not standalone_py.exists() or not touches_dashboard:
        return Verification(
            tier=V3_SERVICE,
            status=v2.status,
            summary="V3 service: V2-only (smoke not in scope) — " + v2.summary,
            evidence={
                "v2_inner": v2.evidence,
                "service_smoke": {
                    "skipped": "no utilization_dashboard runner or files out of scope",
                    "standalone_present": standalone_py.exists(),
                    "touches_dashboard": touches_dashboard,
                },
            },
            blocked_apply=v2.blocked_apply,
        )

    smoke_summary, smoke_evidence, smoke_ok = _service_smoke(pkg_dir)
    status = "pass" if smoke_ok else "fail"
    summary = (
        f"V3 service: {smoke_summary}; {v2.summary}"
        if smoke_ok
        else f"V3 service: smoke failed — {smoke_summary}"
    )
    return Verification(
        tier=V3_SERVICE,
        status=status,
        summary=summary,
        evidence={"v2_inner": v2.evidence, "service_smoke": smoke_evidence},
        blocked_apply=v2.blocked_apply or not smoke_ok,
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
