"""Shared pytest fixtures for the preprocessing test suite."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class FakeTools:
    """Stand-in for OpenDentalMCPTools._query_database.

    Tests register canned responses keyed by SQL prefix. The fake replays them
    in order and records every SQL string seen, so safety-contract tests can
    assert no DML ever leaks.
    """

    rows_by_prefix: dict[str, list[list[dict]]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    fail_with: dict[str, str] = field(default_factory=dict)

    def push_rows(self, sql_prefix: str, rows: list[dict]) -> None:
        self.rows_by_prefix.setdefault(sql_prefix, []).append(rows)

    def fail(self, sql_prefix: str, error: str) -> None:
        self.fail_with[sql_prefix] = error

    def _query_database(self, query: str, limit: int = 1000) -> dict:
        self.queries.append(query)
        for prefix, err in self.fail_with.items():
            if query.lstrip().upper().startswith(prefix.upper()):
                return {"success": False, "error": err}
        for prefix, queue in self.rows_by_prefix.items():
            if query.lstrip().upper().startswith(prefix.upper()) and queue:
                rows = queue.pop(0)
                return {"success": True, "rows": rows}
        return {"success": True, "rows": []}


@pytest.fixture
def fake_tools() -> FakeTools:
    return FakeTools()


@pytest.fixture
def share_root(tmp_path: Path) -> Path:
    """A scratch directory standing in for OD_DOC_ROOT in tests."""
    root = tmp_path / "od_share"
    root.mkdir()
    return root


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "doc_text.db"


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / ".rebuild.lock"
