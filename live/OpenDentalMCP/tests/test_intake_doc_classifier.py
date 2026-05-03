"""Tests for preprocessing.intake.doc_classifier."""

from __future__ import annotations

import json

import pytest

from preprocessing.intake import doc_classifier as dc
from preprocessing.intake import taxonomy as tx


def _factory(response_text: str):
    def call(prompt: str, model: str, max_tokens: int) -> str:
        return response_text
    return call


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_classifies_clean_label() -> None:
    raw = json.dumps({"category": "medical_history", "confidence": 0.92})
    res = dc.classify_document(
        "Past medical conditions...checkbox list...allergies...",
        doc_title="MEDICAL HISTORY",
        caller=_factory(raw),
    )
    assert res.category is tx.MEDICAL_HISTORY
    assert res.category.def_num == 461
    assert res.confidence == pytest.approx(0.92)
    assert res.error is None


def test_all_known_labels_route_correctly() -> None:
    """Every short_label in the taxonomy maps back to its category."""
    for c in tx.ALL_CATEGORIES:
        raw = json.dumps({"category": c.short_label, "confidence": 0.8})
        res = dc.classify_document("text", caller=_factory(raw))
        assert res.category is c, f"Expected {c.short_label}, got {res.category.short_label}"
        assert res.error is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_unknown_label_falls_back_to_miscellaneous() -> None:
    raw = json.dumps({"category": "definitely_not_in_taxonomy", "confidence": 0.99})
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.category is tx.MISCELLANEOUS
    assert res.confidence == 0.0
    assert "unknown_label" in (res.error or "")


def test_missing_category_field() -> None:
    raw = json.dumps({"confidence": 0.5})
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.category is tx.MISCELLANEOUS
    assert res.confidence == 0.0
    assert "unknown_label" in (res.error or "")


def test_response_with_prose_around_json() -> None:
    raw = (
        "Sure, here's the classification:\n"
        '{"category": "consent", "confidence": 0.88}\n'
        "Hope that helps."
    )
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.category is tx.CORRESPONDENCE_CONSENTS
    assert res.confidence == pytest.approx(0.88)


def test_unparseable_response_falls_back() -> None:
    res = dc.classify_document("text", caller=_factory("I cannot help with that."))
    assert res.category is tx.MISCELLANEOUS
    assert res.confidence == 0.0
    assert res.error == "parse_failed"


def test_empty_text_returns_miscellaneous_no_call() -> None:
    called = {"n": 0}

    def boom_caller(*a, **k):
        called["n"] += 1
        return "{}"

    res = dc.classify_document("", caller=boom_caller)
    assert res.category is tx.MISCELLANEOUS
    assert res.error == "empty_text"
    assert called["n"] == 0


def test_caller_raises_returns_safe_result() -> None:
    def boom(*a, **k):
        raise RuntimeError("network down")
    res = dc.classify_document("hello text", caller=boom)
    assert res.category is tx.MISCELLANEOUS
    assert res.confidence == 0.0
    assert "RuntimeError" in (res.error or "")


def test_negative_confidence_clamped() -> None:
    raw = json.dumps({"category": "medical_history", "confidence": -1.5})
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.confidence == 0.0


def test_oversized_confidence_clamped() -> None:
    raw = json.dumps({"category": "medical_history", "confidence": 5.0})
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.confidence == 1.0


def test_bogus_confidence_treated_as_zero() -> None:
    raw = json.dumps({"category": "medical_history", "confidence": "highly confident"})
    res = dc.classify_document("text", caller=_factory(raw))
    assert res.confidence == 0.0


def test_doc_title_included_in_prompt_when_provided() -> None:
    """Sanity check the prompt does include the title hint when given."""
    captured = {}

    def fake(prompt, model, max_tokens):
        captured["prompt"] = prompt
        return json.dumps({"category": "medical_history", "confidence": 0.9})

    dc.classify_document("text", doc_title="MEDICAL HISTORY", caller=fake)
    assert "MEDICAL HISTORY" in captured["prompt"]


def test_doc_title_optional() -> None:
    captured = {}

    def fake(prompt, model, max_tokens):
        captured["prompt"] = prompt
        return json.dumps({"category": "medical_history", "confidence": 0.9})

    dc.classify_document("text", caller=fake)
    # No "Form title" line if title is None
    assert "Form title from the page header" not in captured["prompt"]
