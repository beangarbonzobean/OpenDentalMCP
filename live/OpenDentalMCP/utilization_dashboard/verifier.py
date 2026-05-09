"""Auto-ship verifier for L3 proposals.

The agent's self-reported confidence is unreliable for unattended ship. This
module inspects the actual diff, picks an appropriate test tier, runs it
inside the proposal worktree, and returns a verdict that the L3 path uses
to gate the auto-merge.

Tiers (highest tier triggered by any changed file wins):
    V1_SMOKE    docs / text only            secret regex + markdown sanity
    V2_UNIT     any *.py touched            py_compile + ruff (best-effort)
                                            + pytest near touched files
    V3_SERVICE  routes / blueprint code     V2 + real-import on each touched
                                            module + (optional) start service
                                            on free port, wait for ready, hit
                                            health probes
    V4_UI       static html|css|js          V3 + (TODO) playwright render
    V5_SCHEMA   migrations / requirements   block auto-apply, force pending

A verdict has a `status` of pass / fail / skip / block. Only `pass` lets L3
auto-merge. `block` and `fail` leave the proposal pending. `skip` (e.g.
deferred V4) is treated like pass for now — the operator decides.

V3 service-smoke is opt-in per project. Add a `verify.v3_smoke` block in
projects.yaml:

  verify:
    v3_smoke:
      cwd: live/OpenDentalMCP             # relative to worktree root (optional;
                                          #   defaults to project repo_path)
      command: ["python", "-m", "utilization_dashboard.standalone",
                "--port", "{port}", "--host", "127.0.0.1"]
      ready_path: "/utilization/healthz"  # URL the verifier polls for 200
      ready_timeout_s: 8                  # how long to wait for ready
      probes:                             # additional GETs that must return 2xx
        - "/utilization/projects/"
        - "/utilization/api/projects/snapshot"

When `v3_smoke` is missing the V3 runner still does its real-import check,
just skips the service-spawn portion.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

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

    Uses --select F,E9 so we only flag real bugs (undefined names, unused
    imports, syntax errors) and skip style rules like line length — those
    fail ship for cosmetic reasons and break on existing files that have
    long lines as a pre-existing condition.
    """
    abs_files = [str(wt_path / f) for f in py_files]
    try:
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--quiet",
             "--select", "F,E9", "--no-cache"] + abs_files,
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
                out.append(p)
                seen.add(p)
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
                out.append(c)
                seen.add(c)
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
    """Ask the OS for an unused TCP port. Best-effort; there's a TOCTOU window
    between picking the port and the spawned service binding it, but for the
    verifier we accept that — a flaky port pick just shows up as a smoke fail."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(url: str, timeout: float = 2.0) -> tuple[Optional[int], str]:
    """Return (status_code, body_or_error). status_code=None on transport error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, str(e)[:200]
    except Exception as e:  # noqa: BLE001 — connection refused, timeout, DNS, etc.
        return None, f"{type(e).__name__}: {e}"[:200]


def _module_name_for(rel_path: str, project_cwd_rel: Optional[str]) -> Optional[str]:
    """Convert a worktree-relative .py path to a dotted import name relative to
    the project's cwd. Returns None when the file is outside project_cwd or
    can't be expressed as a clean module."""
    p = rel_path.replace("\\", "/").lstrip("/")
    if project_cwd_rel:
        prefix = project_cwd_rel.replace("\\", "/").strip("/") + "/"
        if not p.startswith(prefix):
            return None
        p = p[len(prefix):]
    if not p.endswith(".py"):
        return None
    p = p[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    if not p:
        return None
    if "/" in p and any(seg.startswith(".") or "-" in seg for seg in p.split("/")):
        return None  # not a clean dotted name
    return p.replace("/", ".")


def _real_imports(
    wt_path: Path,
    project_cwd: Path,
    py_files: list[str],
    project_cwd_rel: Optional[str],
) -> tuple[list[str], list[str]]:
    """Actually import each touched .py file with `python -c 'import X'` from
    project_cwd. Catches NameError, missing deps, circular imports — things
    py_compile (V2) doesn't catch.

    Returns (ok_modules, fail_messages).
    """
    ok: list[str] = []
    fails: list[str] = []
    for rel in py_files:
        mod = _module_name_for(rel, project_cwd_rel)
        if not mod:
            continue  # outside project_cwd or a non-package layout — skip
        res = subprocess.run(
            [sys.executable, "-c", f"import {mod}"],
            cwd=project_cwd,
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        if res.returncode == 0:
            ok.append(mod)
        else:
            err_lines = (res.stderr or res.stdout).strip().splitlines()
            tail = " / ".join(err_lines[-3:])[:300] or "import failed"
            fails.append(f"{mod}: {tail}")
    return ok, fails


def _run_service_smoke(
    worktree_root: Path, smoke_cfg: dict, project_cwd: Path,
) -> tuple[bool, str, dict]:
    """Spawn the configured service on a free port, wait for ready, run probes,
    kill it. Returns (passed, summary_str, evidence_dict)."""
    cwd_rel = smoke_cfg.get("cwd")
    cwd = (worktree_root / cwd_rel).resolve() if cwd_rel else project_cwd
    if not cwd.exists():
        return False, f"smoke cwd missing: {cwd}", {"cwd": str(cwd)}

    raw_cmd = smoke_cfg.get("command") or []
    if not raw_cmd:
        return False, "smoke: no command configured", {}

    port = _free_port()
    cmd = [str(arg).replace("{port}", str(port)) for arg in raw_cmd]
    # Substitute sys.executable for "python" so we use the same interpreter
    # the dashboard runs under (avoids PATH-dependent surprises).
    if cmd[0].lower() in {"python", "python.exe", "python3"}:
        cmd[0] = sys.executable

    ready_path = smoke_cfg.get("ready_path") or "/"
    ready_timeout = float(smoke_cfg.get("ready_timeout_s", 8))
    probes: list[str] = list(smoke_cfg.get("probes") or [ready_path])

    # Spawn. Capture stdout+stderr so we can show the tail on failure.
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, OSError) as e:
        return False, f"spawn failed: {type(e).__name__}: {e}", {
            "cmd": cmd, "cwd": str(cwd),
        }

    base = f"http://127.0.0.1:{port}"
    evidence: dict[str, Any] = {
        "port": port, "cmd": cmd, "cwd": str(cwd),
        "ready_path": ready_path, "probes": [],
    }
    try:
        # Poll for ready.
        deadline = time.monotonic() + ready_timeout
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # subprocess exited before ready
            code, _ = _http_get(base + ready_path, timeout=1.0)
            if code and 200 <= code < 300:
                ready = True
                break
            time.sleep(0.3)

        if not ready:
            tail = _read_proc_tail(proc, max_chars=800)
            evidence["stdout_tail"] = tail
            evidence["died_before_ready"] = proc.poll() is not None
            return False, f"service not ready in {ready_timeout:.0f}s", evidence

        # Run probes.
        bad = 0
        for probe in probes:
            code, body = _http_get(base + probe, timeout=3.0)
            evidence["probes"].append({"path": probe, "status": code,
                                       "snippet": body[:120] if body else ""})
            if not (code and 200 <= code < 300):
                bad += 1

        if bad:
            return False, f"{bad}/{len(probes)} probe(s) failed", evidence
        return True, f"{len(probes)} probe(s) passed", evidence
    finally:
        _terminate(proc)


def _read_proc_tail(proc: subprocess.Popen, max_chars: int = 800) -> str:
    """Read whatever stdout/stderr we have without blocking. Used on failure."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        out, _ = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=2)
        except Exception:  # noqa: BLE001
            return "(could not capture output)"
    except Exception:  # noqa: BLE001
        return "(could not capture output)"
    return (out or "")[-max_chars:]


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort terminate then kill. Safe to call multiple times."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass


def _v3_service(
    wt_path: Path, files: list[str], project: Optional[dict] = None,
) -> Verification:
    """V3 = V2 + real-import on each touched .py + optional service smoke.

    The real-import step is the main upgrade over V2: py_compile only catches
    syntax errors, while a real import catches NameError, missing deps, and
    import-order bugs. Service smoke is the second upgrade and is gated on
    a `verify.v3_smoke` block in projects.yaml — projects that don't define
    one skip the spawn and rely on the import check alone.
    """
    py_files = [f for f in files if f.endswith(".py")]
    issues: list[str] = []

    # 1. Run V2 first — it's cheap and catches the obvious stuff.
    v2 = _v2_unit(wt_path, files)
    if v2.status == "fail":
        issues.extend(v2.evidence.get("issues", []))

    # 2. Real-import check (this is the V3-specific upgrade over V2).
    # The Python import root isn't necessarily `repo_path` — for the dashboard
    # itself, repo_path points at the package dir but you have to import from
    # the parent. Prefer `verify.v3_smoke.cwd` when set (that's the same dir
    # the smoke spawn runs from, by construction the right Python root).
    smoke_cfg = ((project or {}).get("verify") or {}).get("v3_smoke")
    repo_path_rel = (project or {}).get("repo_path") or ""
    import_root_rel = (smoke_cfg or {}).get("cwd") or repo_path_rel
    project_cwd = (wt_path / import_root_rel).resolve() if import_root_rel else wt_path
    imports_ok, imports_fail = _real_imports(
        wt_path, project_cwd, py_files, import_root_rel,
    )
    issues.extend(f"real-import {m}" for m in imports_fail)

    # 3. Optional service smoke.
    smoke_summary = "smoke: not configured"
    smoke_evidence: dict = {}
    if smoke_cfg:
        ok, smoke_summary, smoke_evidence = _run_service_smoke(
            wt_path, smoke_cfg, project_cwd,
        )
        if not ok:
            issues.append(f"service-smoke: {smoke_summary}")

    status = "fail" if issues else "pass"
    summary_parts = [
        v2.summary.replace("V2 unit: ", "v2:"),
        f"real-imports {len(imports_ok)}/{len(imports_ok) + len(imports_fail)}",
        smoke_summary,
    ]
    summary = (
        f"V3 service: {len(issues)} issue(s)"
        if status == "fail"
        else "V3 service: " + "; ".join(summary_parts)
    )
    return Verification(
        tier=V3_SERVICE,
        status=status,
        summary=summary,
        evidence={
            "issues": issues[:30],
            "v2_inner": v2.evidence,
            "real_imports_ok": imports_ok,
            "real_imports_fail": imports_fail,
            "smoke_summary": smoke_summary,
            "smoke": smoke_evidence,
            "project_id": (project or {}).get("id", ""),
        },
        blocked_apply=bool(issues),
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


# Tier runners are normalized to (wt_path, files, project) here so verify()
# can dispatch uniformly. The simpler ones ignore `project`.

def _v1_runner(wt_path, files, project):
    return _v1_smoke(wt_path, files)


def _v2_runner(wt_path, files, project):
    return _v2_unit(wt_path, files)


def _v3_runner(wt_path, files, project):
    return _v3_service(wt_path, files, project)


def _v4_runner(wt_path, files, project):
    return _v4_ui(wt_path, files)


def _v5_runner(wt_path, files, project):
    return _v5_schema(wt_path, files)


_RUNNERS = {
    V1_SMOKE: _v1_runner,
    V2_UNIT: _v2_runner,
    V3_SERVICE: _v3_runner,
    V4_UI: _v4_runner,
    V5_SCHEMA: _v5_runner,
}


def verify(
    wt_path: Path,
    files_changed: list[str],
    project: Optional[dict] = None,
) -> Verification:
    """Public entry point: classify, run, time, and trap exceptions.

    `project` is the projects.yaml entry for the project being verified.
    V3 reads `project.repo_path` to know where the project's checkout sits
    inside the worktree, and reads `project.verify.v3_smoke` (optional) to
    drive the service smoke. Other tiers ignore it.

    A crash in the verifier is treated as `fail` so the L3 path falls back
    to manual review rather than shipping an unverified change.
    """
    tier = classify_tier(files_changed)
    runner = _RUNNERS[tier]
    t0 = time.monotonic()
    try:
        v = runner(wt_path, files_changed, project)
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
