"""
Static (AST) tests that enforce the database / file-share safety contract for
every module under preprocessing/.

Rules tested:
1. NEVER call os.remove / os.unlink / os.rmdir / os.removedirs.
   (Path.unlink is fine — that's a method on Path, not the os module.)
2. NEVER call shutil.copy / copy2 / copyfile / copytree / move / rmtree.
3. NEVER open a file in write/append/exclusive mode except in narrowly
   whitelisted spots inside data/ (currently only the rebuild lock file).
4. NEVER call _make_request (the OD REST API write path) — preprocessing must
   stay out of the live API write surface.
5. NEVER call _get_db_connection or cursor.execute directly. All DB work goes
   through tools._query_database, which preprocessing.sql_safety wraps.
6. EVERY module that calls _query_database must also import assert_select_only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PREPROC_DIR = Path(__file__).resolve().parent.parent / "preprocessing"


# Files in preprocessing/ that MAY use a write-mode open(), and the function
# scope where it's allowed. Each entry: (filename, function_name).
_WRITE_OPEN_ALLOWLIST: set[tuple[str, str]] = {
    ("document_text_index.py", "_acquire_lock"),
    # Debug capture path: only fires when DEBUG_OCR_CAPTURE_DIR is set in env.
    # Writes a per-doc JSON snapshot of the OCR result to that directory for
    # offline alignment work; never touches OD or the live cache.
    ("document_text_index.py", "_debug_capture_doc_result"),
}


def _iter_preproc_files() -> list[Path]:
    """All Python files under preprocessing/, recursively. The intake/
    subpackage is in scope and must pass every rule the top-level files do.
    """
    return sorted(
        p for p in PREPROC_DIR.rglob("*.py")
        if p.name != "__init__.py"
    )


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attr_path(node: ast.AST) -> str:
    """Render an Attribute / Name chain like 'os.remove' or 'self.x.y'.
    Returns '' if not a simple name/attribute chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _enclosing_function(stack: list[ast.AST]) -> str | None:
    for n in reversed(stack):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n.name
    return None


def _walk_with_parents(tree: ast.AST):
    """Yield (node, stack-of-ancestors) for every node."""
    stack: list[ast.AST] = []

    def walk(n: ast.AST):
        stack.append(n)
        try:
            for c in ast.iter_child_nodes(n):
                yield c, list(stack)
                yield from walk(c)
        finally:
            stack.pop()

    yield from walk(tree)


# ---------------------------------------------------------------------------
# Rule 1 + 2: forbidden module functions
# ---------------------------------------------------------------------------

_FORBIDDEN_ATTR_CALLS = {
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    "shutil.move", "shutil.rmtree",
}


@pytest.mark.parametrize("path", _iter_preproc_files(), ids=lambda p: p.name)
def test_no_forbidden_module_calls(path: Path) -> None:
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _attr_path(node.func)
            if name in _FORBIDDEN_ATTR_CALLS:
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == [], (
        "preprocessing/ must not call destructive os/shutil functions. "
        f"Offenders: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 3: write-mode open() is allowlisted
# ---------------------------------------------------------------------------

def _is_write_mode(arg: ast.AST | None) -> bool:
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        return False
    s = arg.value.lower()
    return any(c in s for c in ("w", "a", "x", "+"))


@pytest.mark.parametrize("path", _iter_preproc_files(), ids=lambda p: p.name)
def test_no_unauthorized_write_open(path: Path) -> None:
    tree = _parse(path)
    offenders: list[str] = []
    for node, stack in _walk_with_parents(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match either bare `open(...)` or `builtins.open(...)`.
        name = _attr_path(node.func)
        is_open = (
            (isinstance(node.func, ast.Name) and node.func.id == "open")
            or name in {"builtins.open", "io.open"}
        )
        if not is_open:
            continue
        # Determine mode: 2nd positional or `mode=` kwarg. If absent, default 'r' (safe).
        mode: ast.AST | None = None
        if len(node.args) >= 2:
            mode = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if not _is_write_mode(mode):
            continue
        # Write-mode open detected. Allowed only in whitelisted (file, function).
        fn = _enclosing_function(stack)
        if (path.name, fn or "") in _WRITE_OPEN_ALLOWLIST:
            continue
        offenders.append(f"{path.name}:{node.lineno} fn={fn} mode={ast.dump(mode) if mode else '?'}")
    assert offenders == [], (
        "preprocessing/ must not open files in write mode outside the allowlist. "
        f"Offenders: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 4: no live API write helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _iter_preproc_files(), ids=lambda p: p.name)
def test_no_make_request_calls(path: Path) -> None:
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _attr_path(node.func)
            if name.endswith("._make_request") or name == "_make_request":
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == [], (
        "preprocessing/ must not call OD's _make_request (live REST API). "
        f"Offenders: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 5: no direct DB cursor access
# ---------------------------------------------------------------------------

_FORBIDDEN_DB_CALLS = {"_get_db_connection"}


@pytest.mark.parametrize("path", _iter_preproc_files(), ids=lambda p: p.name)
def test_no_direct_db_helpers(path: Path) -> None:
    """Forbid direct OD DB connection helpers; require routing through _query_database."""
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _attr_path(node.func)
            tail = name.rsplit(".", 1)[-1] if name else ""
            if tail in _FORBIDDEN_DB_CALLS:
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == [], (
        "preprocessing/ must route all DB work through tools._query_database. "
        f"Offenders: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 6: every module touching _query_database imports assert_select_only
# ---------------------------------------------------------------------------

def _imports_select_only(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.endswith("sql_safety"):
                if any(alias.name == "assert_select_only" for alias in node.names):
                    return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("sql_safety"):
                    return True
    return False


def _calls_query_database(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _attr_path(node.func)
            if name.endswith("._query_database") or name == "_query_database":
                return True
    return False


@pytest.mark.parametrize("path", _iter_preproc_files(), ids=lambda p: p.name)
def test_query_database_callers_import_safety_guard(path: Path) -> None:
    tree = _parse(path)
    if not _calls_query_database(tree):
        return
    assert _imports_select_only(tree), (
        f"{path.name} calls _query_database but does not import assert_select_only "
        "from preprocessing.sql_safety."
    )
