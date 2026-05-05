"""Tests for preprocessing.intake.patient_matcher."""

from __future__ import annotations

from typing import Any

import pytest

from preprocessing.intake import patient_matcher as pm


def _factory(by_params: dict[tuple[str, str | None], list[dict]]):
    """Build a fake search_patients that responds based on (last_name, first_name).

    Mirrors tools._search_patients: input keys are snake_case (last_name,
    first_name). The fake routes by (last_name, first_name) into a canned
    response set.
    """
    seen: list[dict] = []

    def call(params: dict) -> list[dict]:
        seen.append(params)
        key = (
            params.get("last_name", "").lower(),
            (params.get("first_name") or "").lower() or None,
        )
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
    """Three-word "First Middle Last" tries both with and without the middle
    so a form-vs-OD middle-name mismatch doesn't break the lookup."""
    pairs = pm.parse_name("Jane Marie Smith")
    assert ("Smith", "Jane Marie") in pairs
    assert ("Smith", "Jane") in pairs  # middle dropped


def test_parse_name_compound_lastname() -> None:
    """'Smith Jr., Jane' should detect Jr. as part of last name."""
    pairs = pm.parse_name("Smith Jr., Jane")
    assert ("Smith Jr.", "Jane") in pairs


def test_parse_name_apostrophe_preserved() -> None:
    """Single-quote in O'Brien isn't a balanced quoted nickname, so it stays."""
    pairs = pm.parse_name("O'Brien, Sean")
    assert ("O'Brien", "Sean") in pairs


def test_parse_name_strips_quoted_nickname() -> None:
    """Balanced single-quoted nicknames are dropped before parsing."""
    pairs = pm.parse_name("Milton, Dan 'Daniel' Ivanovich")
    # Should parse as "Milton, Dan Ivanovich" plus the dropped-middle variant.
    assert ("Milton", "Dan Ivanovich") in pairs
    assert ("Milton", "Dan") in pairs


def test_parse_name_strips_paren_nickname() -> None:
    """Parenthetical nicknames are also dropped."""
    pairs = pm.parse_name("Robert (Bobby) Smith")
    # 3 words after stripping → "Robert Smith"
    assert ("Smith", "Robert") in pairs


def test_parse_name_compound_surname_with_prefix() -> None:
    """OCR splitting "McLaughlin" as "MC LAUGHLIN" should still match.
    Same goes for Mac/De/La/Van prefixes."""
    pairs = pm.parse_name("MARCI MC LAUGHLIN")
    assert ("MC LAUGHLIN", "MARCI") in pairs
    # Without compound handling we'd have (LAUGHLIN, MARCI MC) which will not
    # match "McLaughlin" because the want_first "MARCI MC" disagrees with
    # OD's "Marci". The compound branch is what saves the lookup.


def test_row_name_matches_ignores_internal_whitespace() -> None:
    """'MC LAUGHLIN' should match OD's 'McLaughlin' after normalization."""
    assert pm._row_name_matches(
        {"LName": "McLaughlin", "FName": "Marci"}, "MC LAUGHLIN", "MARCI"
    )
    assert pm._row_name_matches(
        {"LName": "Ginocchio-Nutto", "FName": "Joselyn"}, "Ginocchio Nutto", "Joselyn"
    )


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


# ---------------------------------------------------------------------------
# Defensive name-match: don't trust the API to filter
# ---------------------------------------------------------------------------

def test_uses_snake_case_param_keys() -> None:
    """tools._search_patients reads `last_name` / `first_name`, not `LName`/`FName`.
    Passing CamelCase causes the API to silently return all patients.
    Confirm matcher is calling with the right keys."""
    captured: list[dict] = []

    def search(params: dict) -> list[dict]:
        captured.append(dict(params))
        return [{"PatNum": 101, "LName": "Smith", "FName": "Jane",
                 "Birthdate": "1980-04-12T00:00:00"}]

    pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    assert captured  # at least one call
    for params in captured:
        assert "last_name" in params, f"matcher called with wrong key: {params}"
        assert "LName" not in params, f"matcher used CamelCase: {params}"


def test_unrelated_returned_rows_are_filtered_out() -> None:
    """If the API misbehaves and returns rows not matching the search name,
    the matcher must NOT surface them as a patient. Reproduces the prod
    bug where _search_patients returned all 1000 patients regardless of
    the name filter."""
    def evil_search(params: dict) -> list[dict]:
        # API returns rows for completely different patients
        return [
            {"PatNum": 2, "LName": "ABBACIA", "FName": "YVETTE",
             "Birthdate": "1985-01-01T00:00:00"},
            {"PatNum": 22549, "LName": " Sabou ", "FName": "Bori ",
             "Birthdate": "1981-01-27T00:00:00"},
            {"PatNum": 13662, "LName": ".WEATHERLY", "FName": "STAN",
             "Birthdate": "1950-08-06T00:00:00"},
        ]

    res = pm.match_patient("Hawkins, Lynn", None, search_patients=evil_search)
    assert res.pat_num is None
    assert res.confidence == 0.0
    assert res.reason == "no_od_match"


def test_starts_with_match_accepted() -> None:
    """OD's search may surface partial-prefix rows; matcher accepts those if
    the prefix matches what we asked for."""
    def search(params: dict) -> list[dict]:
        return [{"PatNum": 101, "LName": "Smithson", "FName": "Janet",
                 "Birthdate": "1980-04-12T00:00:00"}]

    res = pm.match_patient("Smith, Jane", "1980-04-12", search_patients=search)
    # Smithson starts with Smith, Janet starts with Jane — accepted
    assert res.pat_num == 101


def test_exact_lname_with_unrelated_fname_falls_back_to_surname_only() -> None:
    """If LName matches but FName clearly doesn't, the primary search rejects
    the row (defensive check). The surname-only fallback then re-surfaces it
    with confidence capped at 0.50 — useful when the form's first name is a
    nickname/abbreviation/spelling-variant of OD's record. Confidence 0.50
    stays well below the auto-file threshold so staff still review it."""
    def search(params: dict) -> list[dict]:
        return [{"PatNum": 999, "LName": "Smith", "FName": "Bob",
                 "Birthdate": "1970-01-01T00:00:00"}]

    res = pm.match_patient("Smith, Jane", None, search_patients=search)
    assert res.pat_num == 999
    assert res.confidence == 0.50
    assert res.reason.startswith("surname_only_fallback:")


def test_surname_only_not_used_when_primary_match_succeeds() -> None:
    """If the primary (last+first) search hits, we never use the surname-only
    fallback — the standard confidence stays at 0.85+."""
    def search(params: dict) -> list[dict]:
        if params.get("first_name") == "Jane":
            return [{"PatNum": 101, "LName": "Smith", "FName": "Jane",
                     "Birthdate": "1980-04-12T00:00:00"}]
        return []
    res = pm.match_patient("Smith, Jane", None, search_patients=search)
    assert res.pat_num == 101
    assert res.confidence >= 0.85
    assert "surname_only_fallback" not in res.reason


def test_surname_only_fallback_multiple_matches_low_conf() -> None:
    """Surname-only fallback hitting multiple Smiths drops confidence further
    via the existing 'multiple near top, no DOB' rule, then the surname-only
    cap pins it at <= 0.50."""
    def search(params: dict) -> list[dict]:
        # Primary calls (last=Patel, first=Thakur) and (last=Thakur, first=Patel)
        # both miss; surname-only "Patel" returns multiple.
        if params.get("first_name"):
            return []
        if params.get("last_name") == "Patel":
            return [
                {"PatNum": 1, "LName": "Patel", "FName": "Thakorbhai",
                 "Birthdate": "1960-01-01T00:00:00"},
                {"PatNum": 2, "LName": "Patel", "FName": "Anita",
                 "Birthdate": "1970-01-01T00:00:00"},
            ]
        return []
    res = pm.match_patient("Thakur Patel", None, search_patients=search)
    assert res.pat_num is not None
    assert res.confidence <= 0.50
    assert res.reason.startswith("surname_only_fallback:")


def test_row_name_matches_helper() -> None:
    rm = pm._row_name_matches
    assert rm({"LName": "Smith", "FName": "Jane"}, "Smith", "Jane") is True
    assert rm({"LName": "smith", "FName": "jane"}, "Smith", "Jane") is True   # case-insens
    assert rm({"LName": " Smith ", "FName": " Jane "}, "Smith", "Jane") is True  # whitespace
    assert rm({"LName": "Smithson", "FName": "Janet"}, "Smith", "Jane") is True  # prefix
    assert rm({"LName": "Smith", "FName": "Janet"}, "Smith", "Jane") is True   # prefix
    assert rm({"LName": "Smith", "FName": "Bob"}, "Smith", "Jane") is False  # fname diff
    assert rm({"LName": "Doe", "FName": "Jane"}, "Smith", "Jane") is False   # lname diff
    assert rm({"LName": "ABBACIA", "FName": "YVETTE"}, "Hawkins", "Lynn") is False
    # No FName supplied: only LName matters
    assert rm({"LName": "Smith", "FName": "anything"}, "Smith", "") is True
    assert rm({"LName": "Doe", "FName": "Smith"}, "Smith", "") is False
