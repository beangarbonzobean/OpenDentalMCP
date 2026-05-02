"""Tests for preprocessing.path_resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from preprocessing.path_resolver import (
    parent_letter,
    patient_folder_name,
    resolve_doc_path,
)


def test_happy_path(tmp_path: Path) -> None:
    p = resolve_doc_path(42, "Young", "Ben", "consent.jpg", share_root=tmp_path)
    assert p == tmp_path / "Y" / "YoungBen42" / "consent.jpg"


def test_unicode_names(tmp_path: Path) -> None:
    p = resolve_doc_path(7, "Núñez", "Álvaro", "id.png", share_root=tmp_path)
    # Parent letter is the first character, uppercased — uppercase is locale-aware
    # but on the systems we run on Núñez -> 'N'.
    assert p == tmp_path / "N" / "NúñezÁlvaro7" / "id.png"


def test_empty_fname(tmp_path: Path) -> None:
    p = resolve_doc_path(99, "Smith", "", "form.pdf", share_root=tmp_path)
    assert p == tmp_path / "S" / "Smith99" / "form.pdf"


def test_single_char_lname(tmp_path: Path) -> None:
    p = resolve_doc_path(1, "x", "Tom", "card.jpg", share_root=tmp_path)
    assert p == tmp_path / "X" / "xTom1" / "card.jpg"


def test_lname_whitespace_is_trimmed(tmp_path: Path) -> None:
    p = resolve_doc_path(8, "  Doe  ", "  Jane  ", "file.jpg", share_root=tmp_path)
    assert p == tmp_path / "D" / "DoeJane8" / "file.jpg"


def test_empty_lname_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_doc_path(1, "", "Bob", "x.jpg", share_root=tmp_path)
    with pytest.raises(ValueError):
        resolve_doc_path(1, "   ", "Bob", "x.jpg", share_root=tmp_path)


def test_empty_filename_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_doc_path(1, "Doe", "Jane", "", share_root=tmp_path)
    with pytest.raises(ValueError):
        resolve_doc_path(1, "Doe", "Jane", "   ", share_root=tmp_path)


def test_patient_folder_name() -> None:
    assert patient_folder_name("Young", "Ben", 42) == "YoungBen42"
    assert patient_folder_name(" Young ", "", 1) == "Young1"


def test_parent_letter() -> None:
    assert parent_letter("Young") == "Y"
    assert parent_letter("núñez") == "N"
    assert parent_letter("  abc") == "A"


def test_default_share_root_used_when_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OD_DOC_ROOT", str(Path("/tmp/od_share")))
    p = resolve_doc_path(5, "Doe", "Jane", "file.jpg")
    assert p == Path("/tmp/od_share") / "D" / "DoeJane5" / "file.jpg"
