"""
Combine per-page LLM extractions into document candidates.

Input: a list of PageExtraction (one per page of a batch PDF, in page order).
Output: a list of DocCandidate, each spanning one or more contiguous pages.

Splitting rules (in order of strength):

  1. Patient name change between consecutive pages -> new document.
     Highest confidence split signal.

  2. Same patient (or unknown patient on this page), but doc_title on the
     current page differs from the previous page's running doc_title -> new
     document. Medium confidence.

  3. is_continuation=True signals "this page is part of the previous form" ->
     append to current doc, regardless of fuzzy mismatches.

  4. Pages where the extractor errored or returned all-null are appended to
     the running doc (conservative: don't split on noise).

Resulting DocCandidate carries the union of patient info gathered across its
pages — first non-null patient_name wins, first non-null patient_dob wins,
the dominant doc_title wins (most pages voting for it).

Pure function. No LLM calls, no I/O. Tests use synthesized PageExtraction
inputs directly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from preprocessing.intake.extractor import PageExtraction


@dataclass
class DocCandidate:
    page_indices: list[int] = field(default_factory=list)
    patient_name: Optional[str] = None
    patient_dob: Optional[str] = None
    doc_title: Optional[str] = None
    split_confidence: float = 1.0  # 0.0 - 1.0; how sure we are this is one doc

    @property
    def page_count(self) -> int:
        return len(self.page_indices)


def split_pages(extractions: Iterable[PageExtraction]) -> list[DocCandidate]:
    """Group a sequence of per-page extractions into contiguous documents."""
    pages = list(extractions)
    if not pages:
        return []

    candidates: list[DocCandidate] = []
    current_pages: list[PageExtraction] = []
    # Tracks the patient/title we believe applies to the current doc so far,
    # taken from the first non-null page that contributed.
    running_name: Optional[str] = None
    running_title: Optional[str] = None
    split_signals: list[float] = []  # confidence per split decision

    def flush() -> None:
        if current_pages:
            cand = _build_candidate(current_pages, split_signals)
            candidates.append(cand)

    for i, page in enumerate(pages):
        is_first = i == 0

        # Decide: does this page start a new doc, or continue the current one?
        starts_new_doc, signal = _decide_split(
            page=page,
            running_name=running_name,
            running_title=running_title,
            is_first=is_first,
        )

        if starts_new_doc and current_pages:
            flush()
            current_pages = []
            running_name = None
            running_title = None
            split_signals = []

        # Append the page to the current doc.
        current_pages.append(page)
        split_signals.append(signal)

        # Update running anchors.
        if running_name is None and _is_real_name(page.patient_name):
            running_name = page.patient_name
        if running_title is None and _is_real_title(page.doc_title):
            running_title = page.doc_title

    flush()
    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decide_split(
    *,
    page: PageExtraction,
    running_name: Optional[str],
    running_title: Optional[str],
    is_first: bool,
) -> tuple[bool, float]:
    """Return (starts_new_doc, confidence-of-this-decision)."""

    if is_first:
        return True, 1.0

    if page.is_continuation:
        # Strong signal: don't split.
        return False, 0.95

    if page.error or (
        page.patient_name is None and page.doc_title is None
    ):
        # Pure noise page; don't split, append conservatively.
        return False, 0.6

    # Patient name change is the strongest split signal.
    if running_name and _is_real_name(page.patient_name) and not _names_match(
        running_name, page.patient_name
    ):
        return True, 1.0

    # Same patient (or unknown on this page), new doc title.
    if running_title and _is_real_title(page.doc_title) and not _titles_match(
        running_title, page.doc_title
    ):
        return True, 0.85

    # Same patient or unknown patient, doc title matches running, or both
    # missing and not continuation: treat as continuation of running doc.
    return False, 0.8


def _build_candidate(
    pages: list[PageExtraction], split_signals: list[float],
) -> DocCandidate:
    name = next((p.patient_name for p in pages if _is_real_name(p.patient_name)), None)
    dob = next((p.patient_dob for p in pages if p.patient_dob), None)

    # Dominant title across the candidate's pages.
    title = _dominant_title(pages)

    # Aggregate split confidence: minimum across boundary decisions, but
    # only those that decided to split or stay (excluding pure-noise 0.6's).
    if split_signals:
        confidence = sum(split_signals) / len(split_signals)
    else:
        confidence = 1.0

    return DocCandidate(
        page_indices=[p.page_idx for p in pages],
        patient_name=name,
        patient_dob=dob,
        doc_title=title,
        split_confidence=round(min(1.0, max(0.0, confidence)), 3),
    )


def _dominant_title(pages: list[PageExtraction]) -> Optional[str]:
    titles = [p.doc_title for p in pages if _is_real_title(p.doc_title)]
    if not titles:
        return None
    counts = Counter(t.strip().lower() for t in titles)
    most_common, _n = counts.most_common(1)[0]
    # Return the original-case version of whichever title voted most.
    for t in titles:
        if t.strip().lower() == most_common:
            return t
    return titles[0]


def _is_real_name(s: Optional[str]) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 2:
        return False
    if s.lower() in {"null", "none", "patient", "n/a"}:
        return False
    return True


def _is_real_title(s: Optional[str]) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 3:
        return False
    if s.lower() in {"null", "none", "n/a"}:
        return False
    return True


def _names_match(a: str, b: str) -> bool:
    """Loose name comparison: case-insensitive, punctuation-insensitive,
    order-insensitive on first/last name pieces."""
    if a is None or b is None:
        return False
    pa = _name_pieces(a)
    pb = _name_pieces(b)
    if not pa or not pb:
        return False
    # If both contain a recognizable token in common, treat as match.
    return bool(pa & pb)


def _name_pieces(s: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in s)
    return {p for p in cleaned.split() if len(p) >= 2}


def _titles_match(a: str, b: str) -> bool:
    """Loose title comparison: any shared substantial word counts."""
    if a is None or b is None:
        return False
    pa = _title_pieces(a)
    pb = _title_pieces(b)
    if not pa or not pb:
        return False
    return bool(pa & pb)


_STOP = {
    "form", "the", "of", "and", "for", "to", "your", "patient",
    "office", "office's", "a", "an", "page", "in",
}


def _title_pieces(s: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in s)
    return {p for p in cleaned.split() if len(p) >= 4 and p not in _STOP}
