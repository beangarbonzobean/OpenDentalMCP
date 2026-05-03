"""Tests for preprocessing.intake.taxonomy."""

from __future__ import annotations

from preprocessing.intake import taxonomy as tx


def test_all_categories_have_unique_def_nums() -> None:
    nums = [c.def_num for c in tx.ALL_CATEGORIES]
    assert len(nums) == len(set(nums)), f"duplicate DefNums: {nums}"


def test_all_categories_have_unique_short_labels() -> None:
    labels = [c.short_label for c in tx.ALL_CATEGORIES]
    assert len(labels) == len(set(labels))


def test_short_labels_are_lowercase_snake() -> None:
    for c in tx.ALL_CATEGORIES:
        assert c.short_label == c.short_label.lower()
        assert " " not in c.short_label


def test_descriptions_nonempty_and_substantive() -> None:
    for c in tx.ALL_CATEGORIES:
        assert c.description.strip()
        assert len(c.description) > 30


def test_by_short_label_known() -> None:
    assert tx.by_short_label("medical_history").def_num == 461
    assert tx.by_short_label("insurance_card").def_num == 462
    assert tx.by_short_label("hipaa").def_num == 459


def test_by_short_label_unknown_returns_miscellaneous() -> None:
    """Wandering LLM output must not break the pipeline."""
    assert tx.by_short_label("not_a_real_label") is tx.MISCELLANEOUS
    assert tx.by_short_label("") is tx.MISCELLANEOUS
    assert tx.by_short_label("Patient Information") is tx.MISCELLANEOUS  # case mismatch


def test_short_labels_method() -> None:
    labels = tx.short_labels()
    assert "patient_info" in labels
    assert "miscellaneous" in labels
    assert len(labels) == len(tx.ALL_CATEGORIES)


def test_def_nums_method() -> None:
    nums = tx.def_nums()
    assert 138 in nums
    assert 461 in nums
    assert 137 in nums
    assert len(nums) == len(tx.ALL_CATEGORIES)


def test_miscellaneous_is_the_fallback_choice() -> None:
    """Should be in the list and have the catch-all DefNum 137."""
    assert tx.MISCELLANEOUS in tx.ALL_CATEGORIES
    assert tx.MISCELLANEOUS.def_num == 137
