"""
Match an extracted patient name + DOB to a PatNum in OD.

Calls OD's /patients endpoint via tools._search_patients() with parsed name
and DOB. Computes a match confidence:

  1.0   : exactly one candidate, both name AND DOB match
  0.95  : multiple candidates, one matches DOB exactly
  0.85  : exactly one candidate by name; we couldn't verify DOB (no DOB
          extracted from the page)
  0.50  : name match but DOB on file disagrees with the extracted DOB
  0.30  : multiple name matches, no DOB to disambiguate
  0.00  : no candidate found

Returns a MatchResult; never raises. The pipeline downstream uses the
confidence to decide auto-file vs queue.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    pat_num: Optional[int]
    label: Optional[str]               # "Lastname, Firstname (PatNum)"
    confidence: float                  # 0.0 - 1.0
    reason: str                        # short explanation, for audit log
    candidates_considered: int = 0     # how many OD candidates we evaluated


def parse_name(extracted: Optional[str]) -> list[tuple[str, str]]:
    """Parse the LLM-extracted name string into possible (LName, FName) pairs.

    Returns at most two candidates:
      - "Lastname, Firstname"   -> [(last, first)]
      - "First Last"            -> [(last, first), (first, last)]  # try both
      - "First Middle Last"     -> [(last, first), (last, "First Middle")]

    Empty / None returns [].
    """
    if not extracted or not isinstance(extracted, str):
        return []
    s = extracted.strip()
    if not s:
        return []

    # "Lastname, Firstname"
    if "," in s:
        last, _, first = s.partition(",")
        last = last.strip()
        first = first.strip()
        if last and first:
            return [(last, first)]

    # "First Last" or "First Middle Last"
    parts = [p for p in re.split(r"\s+", s) if p]
    if len(parts) == 1:
        return [(parts[0], "")]
    if len(parts) == 2:
        first, last = parts
        return [(last, first), (first, last)]  # last-name-first ordering also possible
    if len(parts) >= 3:
        last = parts[-1]
        first = " ".join(parts[:-1])
        return [(last, first)]

    return []


def match_patient(
    extracted_name: Optional[str],
    extracted_dob: Optional[str],
    *,
    search_patients: Callable[[dict], Any],
) -> MatchResult:
    """Look up a patient in OD given an extracted name+DOB.

    `search_patients(params)` is the test seam — typically bound to
    `tools._search_patients`. Production passes that directly. The function
    is expected to return either a list of patient dicts or a dict with a
    list under it (we tolerate both shapes).
    """
    if not extracted_name:
        return MatchResult(
            pat_num=None, label=None, confidence=0.0,
            reason="no_extracted_name",
        )

    candidates = parse_name(extracted_name)
    if not candidates:
        return MatchResult(
            pat_num=None, label=None, confidence=0.0,
            reason="name_unparseable",
        )

    seen: list[dict] = []
    seen_pat_nums: set[int] = set()

    for last, first in candidates:
        params: dict = {"LName": last}
        if first:
            params["FName"] = first
        try:
            raw = search_patients(params)
        except Exception as e:
            log.warning("matcher: search call failed for %r: %s", params, e)
            continue
        rows = _coerce_rows(raw)
        for r in rows:
            try:
                pn = int(r.get("PatNum"))
            except (TypeError, ValueError):
                continue
            if pn in seen_pat_nums:
                continue
            seen_pat_nums.add(pn)
            seen.append(r)

    if not seen:
        return MatchResult(
            pat_num=None, label=None, confidence=0.0,
            reason="no_od_match", candidates_considered=0,
        )

    # Score each candidate.
    scored: list[tuple[dict, float, str]] = []  # (row, score, reason_fragment)
    for r in seen:
        score, frag = _score_candidate(r, extracted_dob)
        scored.append((r, score, frag))

    # Pick the highest score; ties broken by lower PatNum (older record).
    scored.sort(key=lambda t: (-t[1], int(t[0].get("PatNum") or 0)))
    best, best_score, frag = scored[0]
    label = _format_label(best)
    pat_num = int(best.get("PatNum"))

    # Penalize when we have multiple candidates with the same top score and
    # no DOB to disambiguate.
    confidence, reason = _final_confidence(
        best_score, scored, extracted_dob, frag,
    )

    return MatchResult(
        pat_num=pat_num,
        label=label,
        confidence=confidence,
        reason=reason,
        candidates_considered=len(seen),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_candidate(row: dict, extracted_dob: Optional[str]) -> tuple[float, str]:
    """Return (raw_score, reason_fragment) for one OD candidate.

    Raw score scale:
      1.0  - DOB matches exactly
      0.85 - no extracted DOB to compare; name matched
      0.5  - extracted DOB conflicts with this row's Birthdate
    """
    od_dob = _normalize_od_birthdate(row.get("Birthdate"))
    if extracted_dob and od_dob:
        if od_dob == extracted_dob:
            return 1.0, "dob_match"
        return 0.5, "dob_conflict"
    if extracted_dob and not od_dob:
        # OD has no DOB on file; we have one, but can't verify.
        return 0.7, "od_dob_missing"
    if not extracted_dob and od_dob:
        # We have no DOB; can't verify but OD does have one.
        return 0.85, "no_extracted_dob"
    # Both missing.
    return 0.85, "neither_has_dob"


def _final_confidence(
    best_score: float,
    scored: list[tuple[dict, float, str]],
    extracted_dob: Optional[str],
    best_reason: str,
) -> tuple[float, str]:
    """Adjust raw score for ambiguity (multiple candidates, etc.)."""
    if best_score >= 1.0:
        return 1.0, "exact_name_dob_match"
    if best_score >= 0.95:
        return 0.95, "best_of_multiple_dob_match"

    # Check for ambiguity: multiple candidates within 0.05 of best.
    near_top = [s for _, s, _ in scored if abs(s - best_score) < 0.05]
    multiple_near = len(near_top) > 1

    if best_score >= 0.85:
        if multiple_near and not extracted_dob:
            return 0.30, "multiple_name_matches_no_dob"
        if multiple_near:
            return 0.55, "multiple_near_top_with_dob"
        if best_reason == "no_extracted_dob":
            return 0.85, "single_name_match_no_extracted_dob"
        if best_reason == "neither_has_dob":
            return 0.85, "single_name_match_no_dob_anywhere"
        if best_reason == "od_dob_missing":
            return 0.70, "single_name_match_od_dob_missing"
        return 0.85, "single_name_match"

    if best_score >= 0.7:
        return 0.70, "single_name_match_od_dob_missing"

    if best_score >= 0.5:
        return 0.50, "name_match_dob_conflict"

    return 0.30, "weak_match"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_rows(raw: Any) -> list[dict]:
    """OD's _search_patients can return a list, or a dict wrapping a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        # Common shapes: {"patients": [...]} or {"results": [...]} or {"data": [...]}
        for key in ("patients", "results", "data", "rows"):
            v = raw.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # If it looks like a single patient dict, wrap it.
        if "PatNum" in raw:
            return [raw]
    return []


_OD_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def _normalize_od_birthdate(s: Any) -> Optional[str]:
    """OD typically returns Birthdate as 'YYYY-MM-DDT...' or 'YYYY-MM-DD'.
    Sometimes '0001-01-01' for unknown — treat that as None."""
    if not s or not isinstance(s, str):
        return None
    m = _OD_DATE_RE.match(s.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1900:
        return None  # OD's "no DOB on file" sentinel
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _format_label(row: dict) -> str:
    last = (row.get("LName") or "").strip()
    first = (row.get("FName") or "").strip()
    pat_num = row.get("PatNum")
    return f"{last}, {first} ({pat_num})"
