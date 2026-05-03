"""Tests for preprocessing.intake.extractor."""

from __future__ import annotations

import json

import pytest

from preprocessing.intake import extractor as ex


def _factory(response_text: str):
    """Build a fake LLM caller that returns a fixed string."""
    calls = []

    def call(prompt: str, model: str, max_tokens: int) -> str:
        calls.append({"model": model, "max_tokens": max_tokens, "prompt": prompt})
        return response_text

    return call, calls


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_extracts_clean_json_response() -> None:
    payload = json.dumps({
        "patient_name": "Smith, Jane",
        "patient_dob": "1980-04-12",
        "doc_title": "PATIENT INFORMATION FORM",
        "is_continuation": False,
    })
    caller, calls = _factory(payload)
    res = ex.extract_page(
        page_idx=0,
        ocr_text="Patient Information Form ... Name: Jane Smith ... DOB: 04/12/1980",
        caller=caller,
    )
    assert res.patient_name == "Smith, Jane"
    assert res.patient_dob == "1980-04-12"
    assert res.doc_title == "PATIENT INFORMATION FORM"
    assert res.is_continuation is False
    assert res.error is None
    assert len(calls) == 1


def test_extracts_json_inside_prose() -> None:
    """Tolerate models that prepend a 'Here is the JSON:' phrase."""
    response = (
        "Here is the structured output:\n\n"
        '{"patient_name": "Doe, John", "patient_dob": "1975-12-01", '
        '"doc_title": "MEDICAL HISTORY", "is_continuation": false}\n\n'
        "Hope this helps."
    )
    caller, _ = _factory(response)
    res = ex.extract_page(0, "some ocr text", caller=caller)
    assert res.patient_name == "Doe, John"
    assert res.doc_title == "MEDICAL HISTORY"


def test_continuation_page() -> None:
    payload = json.dumps({
        "patient_name": None, "patient_dob": None,
        "doc_title": None, "is_continuation": True,
    })
    caller, _ = _factory(payload)
    res = ex.extract_page(1, "...continued from previous page... signature line", caller=caller)
    assert res.is_continuation is True
    assert res.patient_name is None


def test_dob_format_variants_normalized() -> None:
    """The extractor normalizes DOB inputs even if the LLM returns the raw form."""
    cases = [
        ("1980-04-12", "1980-04-12"),
        ("04/12/1980", "1980-04-12"),
        ("4/12/1980", "1980-04-12"),
        ("4-12-1980", "1980-04-12"),
        ("4/12/80", "1980-04-12"),    # 80 -> 1980
        ("4/12/29", "2029-04-12"),    # 29 -> 2029 (cutoff at 30)
        ("4/12/30", "1930-04-12"),    # 30 -> 1930
        ("invalid",  None),
        ("",          None),
        ("99/99/9999", None),
    ]
    for inp, expected in cases:
        out = ex._normalize_dob(inp)
        assert out == expected, f"{inp!r} -> {out!r} (expected {expected!r})"


# ---------------------------------------------------------------------------
# Error / robustness
# ---------------------------------------------------------------------------

def test_empty_ocr_text_returns_empty_extraction() -> None:
    res = ex.extract_page(0, "", caller=lambda *a, **k: '{"patient_name": "X"}')
    assert res.error == "empty_ocr_text"
    assert res.patient_name is None


def test_whitespace_only_ocr_returns_empty_extraction() -> None:
    res = ex.extract_page(0, "  \n\n\t ", caller=lambda *a, **k: '{}')
    assert res.error == "empty_ocr_text"


def test_caller_raising_returns_error_extraction() -> None:
    def boom(*a, **k):
        raise RuntimeError("network down")
    res = ex.extract_page(0, "some text", caller=boom)
    assert res.error is not None
    assert "RuntimeError" in res.error
    assert res.patient_name is None
    assert res.is_continuation is False


def test_unparseable_response_returns_parse_failed() -> None:
    """A response with no JSON should be flagged but not raise."""
    caller, _ = _factory("I cannot determine that.")
    res = ex.extract_page(0, "some text", caller=caller)
    assert res.error == "parse_failed"
    assert res.patient_name is None


def test_partial_json_handled() -> None:
    """If the JSON is malformed, treated as parse_failed."""
    caller, _ = _factory('{"patient_name": "X", "missing_close')
    res = ex.extract_page(0, "some text", caller=caller)
    assert res.error == "parse_failed"


def test_llm_returns_extra_fields_safely_ignored() -> None:
    payload = json.dumps({
        "patient_name": "Smith, Jane", "patient_dob": "1980-04-12",
        "doc_title": "Consent", "is_continuation": False,
        "extra_garbage": {"foo": "bar"},
    })
    caller, _ = _factory(payload)
    res = ex.extract_page(0, "ocr", caller=caller)
    assert res.patient_name == "Smith, Jane"
    assert res.error is None


def test_llm_returns_invalid_dob_normalized_to_none() -> None:
    payload = json.dumps({
        "patient_name": "Smith, Jane",
        "patient_dob": "Sometime in the 80s",
        "doc_title": "Form", "is_continuation": False,
    })
    caller, _ = _factory(payload)
    res = ex.extract_page(0, "ocr", caller=caller)
    assert res.patient_name == "Smith, Jane"
    assert res.patient_dob is None  # unparseable -> None


def test_long_ocr_text_truncated_in_prompt() -> None:
    long_text = "x" * 20000
    payload = json.dumps({"patient_name": None, "patient_dob": None,
                          "doc_title": None, "is_continuation": False})
    caller, calls = _factory(payload)
    ex.extract_page(0, long_text, caller=caller)
    # Prompt should include the OCR but not the full 20K chars.
    assert len(calls[0]["prompt"]) < 12000
