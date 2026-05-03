"""Tests for preprocessing.intake.patient_matcher."""

from __future__ import annotations

from typing import Any

import pytest

from preprocessing.intake import patient_matcher as pm


def _factory(by_params: dict[tuple[str, str | None], list[dict]]):
    """Build a fake search_patients that responds based on (LName, FName)."""
    seen: list[dict] = []

    def call(params: dict) -> list[dict]:
        seen.append(params)
        key = (params.get("LName", "").lower(), (params.get("FName") or "").lower() or None)
        # Match flexibly: also try (LName, None) if exact key missing
        if key in by_params:
            return by_params[key]
        for k in by_params:
            if k[0] == key[0] and (k[1] is None or k[1] == key[1]):
                return by_params[k]
        return []

    call.seen = seen  # type: ignore[attr-defined]
    return call


def _patient(pat_num: int, lname: str, fname: str, dob: str = "1980-04-12") -> dict:
    return {
        "PatNum": pat_num, "LName": lname, "FName": fname,
        "Birthdate": f"{dob}T00:00:00",
    }


# ---------------------------------------------------------------------------
# parse_name
# ---------------------------------------------------------------------------

def test_parse_name_lastname_first() -> None:
    assert pm.parse_name("Smith, Jane") == [("Smith", "Jane")]


def test_parse_name_first_last() -> None:
    """Two-word names try both orderings."""
    pairs = pm.parse_name("Jane Smith")
    assert ("Smith", "Jane") in pairs
    assert ("Jane", "Smith") in pairs


def test_parse_name_with_middle() -> None:
    assert pm.parse_name("Jane Marie Smith") == [("Smith", "Jane Marie")]


def test_parse_name_compound_lastname() -> None:
    """'Smith Jr., Jane' should detect Jr. as part of last name."""
    assert pm.parse_name("Smith Jr., Jane") == [("Smith Jr.", "Jane")]


def test_parse_name_apostrophe_preserved() -> None:
    assert pm.parse_name("O'Brien, Sean") == [("O'Brien", "Sean")]


def test_parse_name_single_word() -> None:
    assert pm.parse_name("Madonna") == [("Madonna", "")]


def test_parse_name_empty() -> None:
    assert pm.parse_name(None) == []
    assert pm.parse_name("") == []
    assert pm.parse_name("   ") == []


# ---------------------------------------------------------------------------
# match_patient — happy paths
# ---------------------------------------------------------------------------

def test_exact_name_dob_match() -> None:
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="1980-04-12")],
    })
    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num == 101
    assert res.confidence == 1.0
    assert "exact" in res.reason
    assert res.label == "Smith, Jane (101)"


def test_single_name_match_no_extracted_dob() -> None:
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="1980-04-12")],
    })
    res = pm.match_patient("Smith, Jane", None, search_patients=search)
    assert res.pat_num == 101
    assert 0.8 <= res.confidence <= 0.9
    assert "no_extracted_dob" in res.reason


def test_multiple_name_matches_dob_disambiguates() -> None:
    search = _factory({
        ("smith", "jane"): [
            _patient(101, "Smith", "Jane", dob="1985-01-01"),
            _patient(202, "Smith", "Jane", dob="1980-04-12"),
            _patient(303, "Smith", "Jane", dob="1992-06-30"),
        ],
    })
    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num == 202
    assert res.confidence == 1.0
    assert res.candidates_considered == 3


def test_multiple_name_matches_no_dob_low_confidence() -> None:
    search = _factory({
        ("smith", "jane"): [
            _patient(101, "Smith", "Jane", dob="1985-01-01"),
            _patient(202, "Smith", "Jane", dob="1980-04-12"),
        ],
    })
    res = pm.match_patient("Smith, Jane", None, search_patients=search)
    assert res.pat_num is not None  # We pick one but flag low confidence
    assert res.confidence < 0.4
    assert "multiple" in res.reason


def test_dob_conflict_lowers_confidence() -> None:
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="1985-01-01")],
    })
    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num == 101  # still matched
    assert res.confidence == 0.50
    assert "conflict" in res.reason


# ---------------------------------------------------------------------------
# Name-format flexibility
# ---------------------------------------------------------------------------

def test_first_last_format_finds_match_via_alternate_ordering() -> None:
    """OCR returned 'Jane Smith' (first-last). The matcher tries both orderings
    and finds Smith, Jane in OD."""
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="1980-04-12")],
    })
    res = pm.match_patient("Jane Smith", "1980-04-12", search_patients=search)
    assert res.pat_num == 101
    assert res.confidence == 1.0


def test_first_last_format_resolves_via_dob_when_both_orderings_match() -> None:
    """Edge case: 'Jane Smith' matches both ('Smith','Jane') and ('Jane','Smith').
    DOB picks the right one."""
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="1980-04-12")],
        ("jane", "smith"): [_patient(202, "Jane", "Smith", dob="2000-01-01")],
    })
    res = pm.match_patient("Jane Smith", "1980-04-12", search_patients=search)
    assert res.pat_num == 101


# ---------------------------------------------------------------------------
# No-match / failure paths
# ---------------------------------------------------------------------------

def test_no_match_returns_zero_confidence() -> None:
    search = _factory({})
    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num is None
    assert res.confidence == 0.0
    assert res.reason == "no_od_match"


def test_no_extracted_name_short_circuits() -> None:
    called = {"n": 0}

    def boom(params):
        called["n"] += 1
        return []

    res = pm.match_patient(None, "1980-04-12", search_patients=boom)
    assert res.pat_num is None
    assert res.confidence == 0.0
    assert res.reason == "no_extracted_name"
    assert called["n"] == 0


def test_unparseable_name_short_circuits() -> None:
    res = pm.match_patient("   ", "1980-04-12", search_patients=lambda p: [])
    assert res.pat_num is None


def test_search_call_raises_handled() -> None:
    def boom(params):
        raise RuntimeError("network down")

    res = pm.match_patient("Smith, Jane", None, search_patients=boom)
    # Both name orderings tried; both raised. Treated as no match.
    assert res.pat_num is None
    assert res.confidence == 0.0


# ---------------------------------------------------------------------------
# Response shape coercion
# ---------------------------------------------------------------------------

def test_dict_with_patients_key_coerced() -> None:
    def search(params):
        return {"patients": [_patient(101, "Smith", "Jane")]}

    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num == 101


def test_single_dict_response_coerced() -> None:
    def search(params):
        return _patient(101, "Smith", "Jane")

    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num == 101


def test_unknown_response_shape_no_match() -> None:
    def search(params):
        return "garbage"

    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert res.pat_num is None
    assert res.confidence == 0.0


# ---------------------------------------------------------------------------
# Birthdate normalization
# ---------------------------------------------------------------------------

def test_od_zero_birthdate_treated_as_missing() -> None:
    """'0001-01-01' is OD's 'no DOB on file' sentinel."""
    search = _factory({
        ("smith", "jane"): [_patient(101, "Smith", "Jane", dob="0001-01-01")],
    })
    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    # OD has no DOB, we have one — single match, can't verify but accept.
    assert res.pat_num == 101
    assert 0.6 <= res.confidence <= 0.85


def test_od_birthdate_with_time_suffix_parses() -> None:
    """OD often returns '1980-04-12T00:00:00'; the normalizer takes just the date."""
    assert pm._normalize_od_birthdate("1980-04-12T00:00:00") == "1980-04-12"
    assert pm._normalize_od_birthdate("1980-4-12") == "1980-04-12"
    assert pm._normalize_od_birthdate("0001-01-01") is None
    assert pm._normalize_od_birthdate(None) is None
    assert pm._normalize_od_birthdate("") is None
    assert pm._normalize_od_birthdate("not a date") is None
