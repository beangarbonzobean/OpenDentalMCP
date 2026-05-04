"""Tests for preprocessing.intake.page_splitter."""

from __future__ import annotations

from typing import Optional

from preprocessing.intake.extractor import PageExtraction
from preprocessing.intake.page_splitter import (
    DocCandidate,
    split_pages,
    _names_match,
    _titles_match,
)


def _p(
    idx: int,
    *,
    name: Optional[str] = None,
    dob: Optional[str] = None,
    title: Optional[str] = None,
    cont: bool = False,
    err: Optional[str] = None,
) -> PageExtraction:
    return PageExtraction(
        page_idx=idx,
        patient_name=name,
        patient_dob=dob,
        doc_title=title,
        is_continuation=cont,
        error=err,
    )


# ---------------------------------------------------------------------------
# Empty / single-page cases
# ---------------------------------------------------------------------------

def test_empty_input_yields_no_candidates() -> None:
    assert split_pages([]) == []


def test_single_page_one_candidate() -> None:
    res = split_pages([_p(0, name="Smith, Jane", dob="1980-04-12", title="MEDICAL HISTORY")])
    assert len(res) == 1
    assert res[0].page_indices == [0]
    assert res[0].patient_name == "Smith, Jane"
    assert res[0].patient_dob == "1980-04-12"
    assert res[0].doc_title == "MEDICAL HISTORY"


# ---------------------------------------------------------------------------
# Patient-name change splits
# ---------------------------------------------------------------------------

def test_two_patients_two_candidates() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="PATIENT INFORMATION"),
        _p(1, name="Doe, John", title="PATIENT INFORMATION"),
    ])
    assert len(res) == 2
    assert res[0].page_indices == [0]
    assert res[1].page_indices == [1]
    assert res[0].patient_name == "Smith, Jane"
    assert res[1].patient_name == "Doe, John"


def test_three_patients_in_a_row() -> None:
    res = split_pages([
        _p(0, name="A B"), _p(1, name="C D"), _p(2, name="E F"),
    ])
    assert [c.patient_name for c in res] == ["A B", "C D", "E F"]


# ---------------------------------------------------------------------------
# Title-change splits (same patient, different doc)
# ---------------------------------------------------------------------------

def test_same_patient_different_titles_split() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="PATIENT INFORMATION"),
        _p(1, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(2, name="Smith, Jane", title="CONSENT FOR EXTRACTION"),
    ])
    assert len(res) == 3
    assert all(c.patient_name == "Smith, Jane" for c in res)
    assert [c.doc_title for c in res] == [
        "PATIENT INFORMATION", "MEDICAL HISTORY", "CONSENT FOR EXTRACTION",
    ]


# ---------------------------------------------------------------------------
# Continuations
# ---------------------------------------------------------------------------

def test_continuation_keeps_prev_doc() -> None:
    """A page marked is_continuation=True is glued to the prev doc even if
    name/title don't match."""
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, name=None, title=None, cont=True),
        _p(2, name=None, title=None, cont=True),
    ])
    assert len(res) == 1
    assert res[0].page_indices == [0, 1, 2]
    assert res[0].patient_name == "Smith, Jane"


def test_two_page_form_then_new_patient() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, cont=True),
        _p(2, name="Doe, John", title="MEDICAL HISTORY"),
    ])
    assert len(res) == 2
    assert res[0].page_indices == [0, 1]
    assert res[1].page_indices == [2]


# ---------------------------------------------------------------------------
# Noise / errored pages
# ---------------------------------------------------------------------------

def test_errored_page_appended_not_split() -> None:
    """A page with extraction error is treated as 'don't split' to avoid
    fragmenting a doc on noise."""
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, err="parse_failed"),
        _p(2, name="Doe, John", title="MEDICAL HISTORY"),
    ])
    assert len(res) == 2
    assert res[0].page_indices == [0, 1]  # errored page glued to prev
    assert res[1].page_indices == [2]


def test_all_null_page_appended_not_split() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="CONSENT"),
        _p(1),  # all null, no error
    ])
    assert len(res) == 1
    assert res[0].page_indices == [0, 1]


# ---------------------------------------------------------------------------
# Name + title matching (loose)
# ---------------------------------------------------------------------------

def test_loose_name_match_no_split() -> None:
    """Same patient written differently across pages doesn't split."""
    res = split_pages([
        _p(0, name="Jane Smith", title="MEDICAL HISTORY"),
        _p(1, name="Smith, Jane", title="MEDICAL HISTORY"),
    ])
    assert len(res) == 1
    assert res[0].page_indices == [0, 1]


def test_loose_title_match_no_split() -> None:
    """'Medical History Form' vs 'MEDICAL HISTORY' = same doc."""
    res = split_pages([
        _p(0, name="Smith, Jane", title="Medical History Form"),
        _p(1, name="Smith, Jane", title="MEDICAL HISTORY"),
    ])
    assert len(res) == 1


def test_names_match_helper() -> None:
    assert _names_match("Smith, Jane", "Jane Smith") is True
    assert _names_match("Jane Smith", "JANE SMITH") is True
    assert _names_match("Smith, Jane M.", "Jane M. Smith") is True
    assert _names_match("Smith, Jane", "Doe, John") is False
    assert _names_match("Smith, Jane", "") is False
    assert _names_match("", "Smith, Jane") is False


def test_titles_match_helper() -> None:
    assert _titles_match("Medical History Form", "MEDICAL HISTORY") is True
    assert _titles_match("Patient Information", "PATIENT INFO") is False  # 'info' < 4 chars
    assert _titles_match("Medical History", "Patient Information") is False


# ---------------------------------------------------------------------------
# Aggregation across pages
# ---------------------------------------------------------------------------

def test_first_nonnull_dob_wins() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),  # no DOB
        _p(1, name="Smith, Jane", dob="1980-04-12", cont=True),
        _p(2, name="Smith, Jane", dob="1990-01-01", cont=True),  # ignored
    ])
    assert len(res) == 1
    assert res[0].patient_dob == "1980-04-12"


def test_dominant_title_voted_when_pages_disagree() -> None:
    """If a multi-page form mostly says one title, that wins for the candidate."""
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, name="Smith, Jane", title="MEDICAL HISTORY", cont=True),
        _p(2, name="Smith, Jane", title="MED HIST", cont=True),  # OCR variant
    ])
    assert len(res) == 1
    assert "MEDICAL HISTORY" in res[0].doc_title


def test_split_confidence_is_a_real_number() -> None:
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, name="Doe, John", title="MEDICAL HISTORY"),
    ])
    for c in res:
        assert 0.0 <= c.split_confidence <= 1.0


# ---------------------------------------------------------------------------
# Edge: page with no name but a title
# ---------------------------------------------------------------------------

def test_unknown_patient_doc_not_promoted_to_running_name() -> None:
    """A page that introduces a new title but has no patient name is treated
    as a new doc (title change), but we don't fabricate a patient name for it."""
    res = split_pages([
        _p(0, name="Smith, Jane", title="MEDICAL HISTORY"),
        _p(1, name=None, title="HIPAA NOTICE"),
    ])
    assert len(res) == 2
    # The HIPAA page stayed nameless; the splitter doesn't infer.
    assert res[1].patient_name is None
