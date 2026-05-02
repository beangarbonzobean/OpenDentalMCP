"""
Pre-flight checks for the document_text_index pilot.

Verifies the environment is healthy enough to run a backfill:
  - data/ directory is writable
  - the SQLite cache file can be opened in WAL mode
  - tools._query_database returns rows for a trivial SELECT
  - the OD image share is readable (we don't read every doc — just probe one)
  - free disk space margin
  - enumerates DocCategory definitions so the user can pick the skip-set

Read-only against OD's database. Writes only to the local data/ directory.

Usage from a Python session:
    from preprocessing import preflight
    report = preflight.run(tools)
    print(preflight.format_report(report))
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from preprocessing import document_text_cache as cache
from preprocessing.path_resolver import _share_root
from preprocessing.sql_safety import assert_select_only


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CategoryRow:
    DefNum: int
    ItemName: str
    ItemValue: Optional[str] = None


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)
    categories: list[CategoryRow] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)


# Open Dental's `definition` table. Category=18 is DocCategory in stock OD.
_DOC_CATEGORY_DEFCAT = int(os.environ.get("DOC_CATEGORY_DEFCAT", "18"))


def _check_data_dir_writable() -> CheckResult:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return CheckResult("data_dir_writable", True, str(data_dir))
    except Exception as e:
        return CheckResult("data_dir_writable", False, f"{e}")


def _check_cache_opens() -> CheckResult:
    try:
        p = cache.init_cache()
        with cache.open_cache(p) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                return CheckResult("cache_wal_mode", False, f"got {mode!r}")
        return CheckResult("cache_wal_mode", True, str(p))
    except Exception as e:
        return CheckResult("cache_wal_mode", False, f"{e}")


def _check_disk_space(min_free_gb: float = 1.0) -> CheckResult:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    try:
        usage = shutil.disk_usage(str(data_dir.parent))
        free_gb = usage.free / 1024 / 1024 / 1024
        if free_gb < min_free_gb:
            return CheckResult("disk_space", False, f"only {free_gb:.2f} GB free")
        return CheckResult("disk_space", True, f"{free_gb:.2f} GB free")
    except Exception as e:
        return CheckResult("disk_space", False, f"{e}")


def _check_share_root_exists() -> CheckResult:
    root = _share_root()
    try:
        if not root.exists():
            return CheckResult("share_root_exists", False, f"missing: {root}")
        return CheckResult("share_root_exists", True, str(root))
    except Exception as e:
        return CheckResult("share_root_exists", False, f"{e}")


def _check_db_select(tools: Any) -> CheckResult:
    sql = "SELECT 1 AS ok"
    try:
        assert_select_only(sql)
        result = tools._query_database(sql, limit=1)
        if isinstance(result, dict) and not result.get("success", True):
            return CheckResult("db_select", False, str(result.get("error")))
        rows = result.get("rows", []) if isinstance(result, dict) else result
        if not rows:
            return CheckResult("db_select", False, "no rows")
        return CheckResult("db_select", True, "SELECT 1 ok")
    except Exception as e:
        return CheckResult("db_select", False, f"{e}")


def _enumerate_doc_categories(tools: Any) -> tuple[CheckResult, list[CategoryRow]]:
    sql = (
        "SELECT DefNum, ItemName, ItemValue FROM definition "
        "WHERE Category = ? AND IsHidden = 0 ORDER BY ItemOrder"
    )
    assert_select_only(sql)
    rendered = sql.replace("?", str(int(_DOC_CATEGORY_DEFCAT)), 1)
    try:
        result = tools._query_database(rendered, limit=1000)
        if isinstance(result, dict) and not result.get("success", True):
            return (
                CheckResult("enumerate_doc_categories", False, str(result.get("error"))),
                [],
            )
        rows = result.get("rows", []) if isinstance(result, dict) else result
        cats = [
            CategoryRow(
                DefNum=int(r["DefNum"]),
                ItemName=str(r.get("ItemName") or ""),
                ItemValue=(str(r["ItemValue"]) if r.get("ItemValue") is not None else None),
            )
            for r in rows
        ]
        return CheckResult("enumerate_doc_categories", True, f"{len(cats)} categories"), cats
    except Exception as e:
        return CheckResult("enumerate_doc_categories", False, f"{e}"), []


def run(tools: Any) -> PreflightReport:
    rep = PreflightReport()
    rep.checks.append(_check_data_dir_writable())
    rep.checks.append(_check_cache_opens())
    rep.checks.append(_check_disk_space())
    rep.checks.append(_check_share_root_exists())
    rep.checks.append(_check_db_select(tools))
    cat_check, cats = _enumerate_doc_categories(tools)
    rep.checks.append(cat_check)
    rep.categories = cats
    return rep


_RAY_HINT_TOKENS = ("xray", "x-ray", "radio", "panor", "pano", "ceph", "bitewing", "bw")


def suggest_skip_categories(report: PreflightReport) -> list[CategoryRow]:
    """Heuristic: any DocCategory whose name suggests radiograph imagery is a
    candidate for the skip-set. The user has the final say."""
    out: list[CategoryRow] = []
    for c in report.categories:
        name = c.ItemName.lower()
        if any(tok in name for tok in _RAY_HINT_TOKENS):
            out.append(c)
    return out


def format_report(report: PreflightReport) -> str:
    lines: list[str] = []
    lines.append("=== Preflight checks ===")
    for c in report.checks:
        marker = "OK" if c.ok else "FAIL"
        lines.append(f"  [{marker}] {c.name}: {c.detail}")
    lines.append("")
    lines.append(f"=== DocCategory definitions ({len(report.categories)}) ===")
    for c in report.categories:
        lines.append(f"  {c.DefNum:>4}  {c.ItemName}")
    suggested = suggest_skip_categories(report)
    if suggested:
        ids = ",".join(str(c.DefNum) for c in suggested)
        lines.append("")
        lines.append("Suggested DOC_TEXT_SKIP_CATEGORIES (review before using):")
        lines.append(f"  {ids}")
        for c in suggested:
            lines.append(f"    - {c.DefNum}  {c.ItemName}")
    lines.append("")
    overall = "READY" if report.all_ok else "BLOCKED — fix failures above"
    lines.append(f"Overall: {overall}")
    return "\n".join(lines)


# Allow running as a module: python -m preprocessing.preflight
if __name__ == "__main__":  # pragma: no cover
    # When run directly we need a real tools instance — import is deferred so
    # tests can monkeypatch.
    import importlib
    mcp = importlib.import_module("mcp_tools")
    tools = mcp.OpenDentalMCPTools()  # type: ignore[attr-defined]
    print(format_report(run(tools)))
