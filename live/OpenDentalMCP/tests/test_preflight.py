"""Tests for preprocessing.preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from preprocessing import preflight

from tests.conftest import FakeTools


def test_check_data_dir_writable_ok() -> None:
    r = preflight._check_data_dir_writable()
    assert r.ok is True
    assert "data" in r.detail.lower()


def test_check_cache_opens_in_wal() -> None:
    r = preflight._check_cache_opens()
    assert r.ok is True


def test_check_db_select_happy(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [{"ok": 1}])
    r = preflight._check_db_select(fake_tools)
    assert r.ok is True


def test_check_db_select_failure(fake_tools: FakeTools) -> None:
    fake_tools.fail("SELECT", "auth failed")
    r = preflight._check_db_select(fake_tools)
    assert r.ok is False
    assert "auth" in r.detail


def test_enumerate_doc_categories(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [
        {"DefNum": 1, "ItemName": "Insurance Card", "ItemValue": None},
        {"DefNum": 2, "ItemName": "Bitewing X-Ray", "ItemValue": None},
        {"DefNum": 3, "ItemName": "Consent Form", "ItemValue": None},
        {"DefNum": 4, "ItemName": "Panoramic", "ItemValue": None},
    ])
    chk, cats = preflight._enumerate_doc_categories(fake_tools)
    assert chk.ok is True
    assert len(cats) == 4
    assert cats[0].ItemName == "Insurance Card"


def test_suggest_skip_categories_picks_xray_like() -> None:
    rep = preflight.PreflightReport(
        checks=[],
        categories=[
            preflight.CategoryRow(DefNum=1, ItemName="Insurance Card"),
            preflight.CategoryRow(DefNum=2, ItemName="Bitewing X-Ray"),
            preflight.CategoryRow(DefNum=3, ItemName="Panoramic Image"),
            preflight.CategoryRow(DefNum=4, ItemName="Cephalometric"),
            preflight.CategoryRow(DefNum=5, ItemName="ID"),
        ],
    )
    suggested = preflight.suggest_skip_categories(rep)
    assert {c.DefNum for c in suggested} == {2, 3, 4}


def test_format_report_renders_status(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [{"ok": 1}])  # _check_db_select
    fake_tools.push_rows("SELECT", [
        {"DefNum": 1, "ItemName": "Bitewing X-Ray", "ItemValue": None},
    ])
    rep = preflight.run(fake_tools)
    text = preflight.format_report(rep)
    assert "Preflight checks" in text
    assert "DocCategory" in text


def test_run_marks_all_ok_or_not(fake_tools: FakeTools) -> None:
    fake_tools.push_rows("SELECT", [{"ok": 1}])
    fake_tools.push_rows("SELECT", [])
    rep = preflight.run(fake_tools)
    # Not asserting all_ok specifically — share path may not exist on test box.
    # Just confirm structure is sane.
    assert isinstance(rep.all_ok, bool)
    assert any(c.name == "db_select" for c in rep.checks)
    assert any(c.name == "enumerate_doc_categories" for c in rep.checks)
