"""Tests for preprocessing.path_resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from preprocessing.path_resolver import (
    parent_letter,
    patient_folder_name,
    resolve_doc_path,
    resolve_doc_path_with_fallback,
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


# --- OD-style sanitization (hyphens, spaces, periods, quotes stripped) ----

def test_hyphen_in_lname_stripped(tmp_path: Path) -> None:
    """OD strips hyphens from names when forming folder names."""
    p = resolve_doc_path(18674, "EDWARDS-GRAY", "ADEHRRA", "file.pdf", share_root=tmp_path)
    assert p == tmp_path / "E" / "EDWARDSGRAYADEHRRA18674" / "file.pdf"


def test_hyphen_in_fname_stripped(tmp_path: Path) -> None:
    p = resolve_doc_path(99, "Smith", "Mary-Jane", "file.pdf", share_root=tmp_path)
    assert p == tmp_path / "S" / "SmithMaryJane99" / "file.pdf"


def test_period_and_double_space_stripped(tmp_path: Path) -> None:
    """Real example from OD: 'LOUTON  JR.KEITH' -> 'LOUTONJRKEITH'."""
    p = resolve_doc_path(20944, "LOUTON  JR.", "KEITH", "file.pdf", share_root=tmp_path)
    assert p == tmp_path / "L" / "LOUTONJRKEITH20944" / "file.pdf"


def test_quote_stripped(tmp_path: Path) -> None:
    """Real example from OD: MARKMAN, GLORIA \"JAZZ\"."""
    p = resolve_doc_path(72, 'MARKMAN', 'GLORIA "JAZZ"', "file.pdf", share_root=tmp_path)
    assert p == tmp_path / "M" / "MARKMANGLORIAJAZZ72" / "file.pdf"


def test_space_in_lname_stripped(tmp_path: Path) -> None:
    """Real example: 'ZAMACONA HERNANDEZ' + 'ANDREA' -> 'ZAMACONAHERNANDEZANDREA'."""
    p = resolve_doc_path(19042, "ZAMACONA HERNANDEZ", "ANDREA", "file.pdf", share_root=tmp_path)
    assert p == tmp_path / "Z" / "ZAMACONAHERNANDEZANDREA19042" / "file.pdf"


def test_only_punctuation_lname_raises(tmp_path: Path) -> None:
    """If the LName is entirely punctuation it sanitizes to empty -> ValueError."""
    with pytest.raises(ValueError):
        resolve_doc_path(1, '"-..-"', "Bob", "x.jpg", share_root=tmp_path)


def test_parent_letter_handles_leading_punctuation() -> None:
    """A leading hyphen shouldn't break parent-letter detection."""
    assert parent_letter("-Smith") == "S"
    assert parent_letter('"Doe"') == "D"


def test_patient_folder_name_strips() -> None:
    assert patient_folder_name("EDWARDS-GRAY", "ADEHRRA", 18674) == "EDWARDSGRAYADEHRRA18674"
    assert patient_folder_name("De La Vega", "Emily", 21495) == "DeLaVegaEmily21495"


# --- Fuzzy PatNum fallback ----------------------------------------------

def test_fallback_finds_folder_when_constructed_path_missing(tmp_path: Path) -> None:
    """If the constructed path doesn't exist, scan parent letter dir for a
    folder ending in PatNum and use it."""
    # Real folder name on disk doesn't match what DB-derived sanitization yields.
    actual = tmp_path / "B" / "BeltranHernan21199"
    actual.mkdir(parents=True)
    (actual / "file.pdf").write_bytes(b"x")
    # DB has a typo so even after sanitize the constructed name differs.
    p = resolve_doc_path_with_fallback(21199, "Beltean", "Hernan", "file.pdf", share_root=tmp_path)
    assert p == actual / "file.pdf"


def test_fallback_returns_constructed_when_no_match(tmp_path: Path) -> None:
    """When the file is genuinely missing, return the primary path so the
    error message points at where we looked."""
    primary = resolve_doc_path(42, "Smith", "Bob", "file.pdf", share_root=tmp_path)
    p = resolve_doc_path_with_fallback(42, "Smith", "Bob", "file.pdf", share_root=tmp_path)
    assert p == primary


def test_fallback_skips_when_constructed_exists(tmp_path: Path) -> None:
    """If primary path exists, fallback is a no-op even if other folders match."""
    primary_dir = tmp_path / "S" / "SmithBob42"
    primary_dir.mkdir(parents=True)
    (primary_dir / "file.pdf").write_bytes(b"x")
    # A "decoy" folder also ends in 42, but should be ignored.
    decoy = tmp_path / "S" / "OtherPatient42"
    decoy.mkdir(parents=True)
    p = resolve_doc_path_with_fallback(42, "Smith", "Bob", "file.pdf", share_root=tmp_path)
    assert p == primary_dir / "file.pdf"


def test_fallback_skips_ambiguous_match(tmp_path: Path) -> None:
    """If multiple folders end in PatNum, refuse to pick (return primary path)."""
    (tmp_path / "S" / "SmithBob42").mkdir(parents=True)
    (tmp_path / "S" / "AnotherPat42").mkdir(parents=True)
    primary = resolve_doc_path(42, "Smith", "Robert", "file.pdf", share_root=tmp_path)
    p = resolve_doc_path_with_fallback(42, "Smith", "Robert", "file.pdf", share_root=tmp_path)
    assert p == primary  # neither candidate has the file, fallback returns primary


def test_fallback_does_not_match_partial_patnum_suffix(tmp_path: Path) -> None:
    """PatNum 1234 should NOT match folder ending in '51234' or '01234' — must
    be the actual PatNum, with non-digit (or nothing) before it."""
    (tmp_path / "S" / "OtherPatient51234").mkdir(parents=True)
    (tmp_path / "S" / "OtherPatient51234" / "file.pdf").write_bytes(b"x")
    primary = resolve_doc_path(1234, "Smith", "Bob", "file.pdf", share_root=tmp_path)
    p = resolve_doc_path_with_fallback(1234, "Smith", "Bob", "file.pdf", share_root=tmp_path)
    assert p == primary  # decoy not picked


def test_fallback_handles_missing_parent_dir(tmp_path: Path) -> None:
    """If the parent letter directory doesn't exist at all, fallback returns
    primary without crashing."""
    primary = resolve_doc_path(7, "Zzz", "Top", "file.pdf", share_root=tmp_path)
    p = resolve_doc_path_with_fallback(7, "Zzz", "Top", "file.pdf", share_root=tmp_path)
    assert p == primary
