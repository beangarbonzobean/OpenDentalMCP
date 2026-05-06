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


_NICKNAME_RE = re.compile(r"['\"\(\[][^'\"\)\]]+['\"\)\]]")


# Suffix tokens that should be folded into the previous word as part of the
# surname rather than treated as the surname itself. Lowercased keys; we
# match case-insensitively and tolerate trailing periods.
_NAME_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "v", "2nd", "3rd", "4th",
}


def _is_suffix(token: str) -> bool:
    return token.strip(" .").lower() in _NAME_SUFFIXES


def _strip_nicknames(s: str) -> str:
    """Drop nicknames in quotes or parens before parsing.

    Examples: "Dan 'Daniel' Milton"  -> "Dan Milton"
              "Robert (Bobby) Smith" -> "Robert Smith"
    Empties left behind by the strip are collapsed.
    """
    return re.sub(r"\s+", " ", _NICKNAME_RE.sub(" ", s)).strip()


def parse_name(extracted: Optional[str]) -> list[tuple[str, str]]:
    """Parse the LLM-extracted name string into possible (LName, FName) pairs.

    Returns multiple candidates so the matcher can try several until one hits:
      - "Lastname, Firstname"      -> [(last, first), (last, "")]
      - "First Last"               -> [(last, first), (first, last), (last, "")]
      - "First Middle Last"        -> [(last, first), (last, "first middle"),
                                       (last, first_word_only), (last, "")]

    A trailing surname-only candidate `(last, "")` is appended on every path so
    the matcher can broaden the search when the first/middle name on the form
    differs from what's in OD (nickname, abbreviation, spelling variant).

    Nicknames in quotes/parens are stripped first so "Dan 'Daniel' Milton"
    parses cleanly as "Dan Milton".

    Empty / None returns [].
    """
    if not extracted or not isinstance(extracted, str):
        return []
    s = _strip_nicknames(extracted.strip())
    if not s:
        return []

    out: list[tuple[str, str]] = []

    def _push(pair: tuple[str, str]) -> None:
        if pair and pair not in out:
            out.append(pair)

    if "," in s:
        last, _, first = s.partition(",")
        last = last.strip()
        first = first.strip()
        if last and first:
            _push((last, first))
            # Also try surname + first word of given-name (drops middle name(s)).
            first_only = first.split()[0] if first else ""
            if first_only and first_only != first:
                _push((last, first_only))
        elif last:
            _push((last, ""))
        return out

    parts = [p for p in re.split(r"\s+", s) if p]
    # Fold a trailing generational suffix (Jr., Sr., II, III, ...) into the
    # previous token so "KEITH LOUTON JR." becomes ["KEITH", "LOUTON JR."].
    # OD typically stores the suffix as part of the surname. Track this so
    # we know the ordering is unambiguous (the suffix-bearing word is the
    # surname) and skip the (first, last) swap below.
    suffix_detected = False
    if len(parts) >= 2 and _is_suffix(parts[-1]):
        parts = parts[:-2] + [parts[-2] + " " + parts[-1]]
        suffix_detected = True
    if len(parts) == 1:
        _push((parts[0], ""))
        return out
    if len(parts) == 2:
        first, last = parts
        _push((last, first))
        if not suffix_detected:
            # Without a suffix, "First Last" is ambiguous (could be ordered
            # the other way). With a suffix the surname is unambiguous, so
            # don't emit the swap — it would send the surname-only fallback
            # chasing patients with the *first* name as a surname.
            _push((first, last))
        return out
    if len(parts) >= 3:
        last = parts[-1]
        first_full = " ".join(parts[:-1])
        first_only = parts[0]
        _push((last, first_full))
        if first_only != first_full:
            _push((last, first_only))   # drop middle name(s)
        # Compound surnames where the OCR/extractor inserted a space between
        # the prefix and the root: "MARCI MC LAUGHLIN" -> "Mc Laughlin",
        # "Maria DE LA ROSA" -> "De La Rosa". Try the prefix joined with the
        # final word as a single surname so _row_name_matches' whitespace-
        # flexible compare can hit "McLaughlin" / "DeLaRosa" in OD.
        if parts[-2].lower() in _COMPOUND_SURNAME_PREFIXES:
            compound_last = parts[-2] + " " + parts[-1]
            compound_first_full = " ".join(parts[:-2])
            _push((compound_last, compound_first_full))
            if parts[0] != compound_first_full:
                _push((compound_last, parts[0]))
        return out

    return out


_COMPOUND_SURNAME_PREFIXES = {
    "mc", "mac", "o", "o'", "de", "del", "la", "le", "van", "von", "san", "st", "st.",
    "der", "den", "ten", "ter",
}


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

    def _try(last: str, first: str, *, strict: bool = False) -> None:
        # NOTE: tools._search_patients reads `last_name` / `first_name`
        # (snake_case) and translates them to OD's `LName`/`FName`. Calling
        # with the CamelCase keys gets silently dropped and OD returns ALL
        # patients. Always use snake_case here.
        params: dict = {"last_name": last}
        if first:
            params["first_name"] = first
        try:
            raw = search_patients(params)
        except Exception as e:
            log.warning("matcher: search call failed for %r: %s", params, e)
            return
        rows = _coerce_rows(raw)
        for r in rows:
            try:
                pn = int(r.get("PatNum"))
            except (TypeError, ValueError):
                continue
            if pn in seen_pat_nums:
                continue
            # Defensive: require the returned row's name to actually match
            # the search terms (case-insensitive). Even if the API silently
            # drops a filter, the matcher won't surface an unrelated patient.
            if not _row_name_matches(r, last, first, strict=strict):
                continue
            seen_pat_nums.add(pn)
            seen.append(r)

    for last, first in candidates:
        _try(last, first)

    # Surname-only fallback. Triggered only if every (last, first) attempt
    # came back empty. Catches cases where the form's first name doesn't
    # match what's in OD (nicknames, abbreviations, spelling variants):
    # e.g. extracted "Thakur Patel" vs OD's "PATEL, THAKORBHAI". Confidence
    # is capped below so these never auto-file — they always queue. Uses
    # strict=True on _row_name_matches so a search for surname "Robert"
    # doesn't match OD's LName="Roberto" via prefix.
    surname_only_used = False
    if not seen:
        tried_lasts: set[str] = set()
        for last, _first in candidates:
            key = _normalize_for_compare(last)
            if not key or key in tried_lasts:
                continue
            tried_lasts.add(key)
            before = len(seen)
            _try(last, "", strict=True)
            if len(seen) > before:
                surname_only_used = True

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

    # Surname-only matches are weak signals; cap below auto-file threshold
    # and tag the reason so audit reviewers see why the row was queued.
    if surname_only_used:
        if confidence > 0.50:
            confidence = 0.50
        reason = f"surname_only_fallback:{reason}"

    # Surname-collision check: even when we got exactly one match in the
    # primary search and returned a confident 0.85, OD may simply not have
    # surfaced the second patient with that name in the (last+first) query
    # — e.g. "Shu Chen" returned only PatNum 82 but the actual patient
    # 22313 was a separate Shu Chen the search missed. When we have no DOB
    # to verify with, downgrade single-hit name matches to 0.70 if more
    # than one OD record shares the surname. That keeps these in the
    # review queue rather than auto-filing the wrong record.
    if (
        not surname_only_used
        and not extracted_dob
        and confidence >= 0.85
        and len(seen) == 1
    ):
        surname_for_check = (best.get("LName") or "").strip()
        if surname_for_check:
            try:
                raw_all = search_patients({"last_name": surname_for_check})
                all_rows = _coerce_rows(raw_all)
                count_strict = sum(
                    1 for r in all_rows
                    if _row_name_matches(r, surname_for_check, "", strict=True)
                )
            except Exception as e:
                log.warning("matcher: surname collision check failed: %s", e)
                count_strict = 1
            if count_strict > 1:
                confidence = 0.70
                reason = f"surname_collision_no_dob:{reason}:siblings={count_strict}"

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


def _normalize_for_compare(s: str) -> str:
    """Normalize a name field for comparison: lowercase + collapse internal
    whitespace + strip non-alphanumerics. Lets "Mc Laughlin" match
    "McLaughlin" and "O'Brian" match "Obrian"."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _row_name_matches(
    row: dict, want_last: str, want_first: str, *, strict: bool = False,
) -> bool:
    """Defensive: confirm the returned OD row matches the name we asked for.

    Comparison strips whitespace and non-alphanumerics on both sides so
    "MC LAUGHLIN" matches "McLaughlin", "O'brian" matches "Obrian", etc.
    For the first name, we accept either an exact normalized match or a
    starts-with so that "Tanya" still matches "Tanya M." or just "T".
    Empty want_first means we don't constrain the first-name side (used by
    the surname-only fallback search).

    `strict=True` requires *exact* normalized equality on the surname (no
    prefix in either direction). The surname-only fallback uses this to
    avoid false-positive cross-field matches — e.g. searching for surname
    "Robert" must not match an OD record where LName="ROBERTO". Prefix-
    flexibility is preserved on first names (and on surnames in non-strict
    mode) so OCR truncations and middle-initial trailers still work.
    """
    row_last_n = _normalize_for_compare(row.get("LName") or "")
    row_first_n = _normalize_for_compare(row.get("FName") or "")
    want_last_n = _normalize_for_compare(want_last)
    want_first_n = _normalize_for_compare(want_first)
    if not want_last_n:
        return False
    if strict:
        if row_last_n != want_last_n:
            return False
    else:
        if row_last_n != want_last_n and not row_last_n.startswith(want_last_n) \
                and not want_last_n.startswith(row_last_n):
            return False
    if want_first_n:
        if row_first_n != want_first_n and not row_first_n.startswith(want_first_n) \
                and not want_first_n.startswith(row_first_n):
            return False
    return True
