"""
Resolve a document's on-disk path under the Open Dental image share.

Open Dental stores patient documents under `<share>/<L>/<LName><FName><PatNum>/<FileName>`,
where `<L>` is the uppercased first letter of the patient's last name.

OD strips all non-alphanumeric characters (hyphens, spaces, periods, quotes,
commas, etc.) from the patient name parts before forming the folder name, so
e.g. `EDWARDS-GRAY` + `ADEHRRA` becomes `EDWARDSGRAYADEHRRA`. We replicate that
behavior; otherwise nightly OCR pipeline gets a flood of `not_found` errors for
every patient with a punctuated or hyphenated name.

The share root is taken from the OD_DOC_ROOT environment variable, defaulting to
`\\SERVER12\OpenDentImages` (matching route_slip_ocr.py's existing behavior).

This module is read-only: it only constructs and inspects paths. It never writes,
moves, or deletes files. Tests in tests/test_safety_contract.py enforce that.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


def _share_root() -> Path:
    return Path(os.environ.get("OD_DOC_ROOT", r"\\SERVER12\OpenDentImages"))


# Strip non-word chars + underscore. `\w` is Unicode-aware in Python 3, so
# this preserves Unicode letters (ñ, é, etc.) while stripping hyphens, spaces,
# periods, quotes, commas, parentheses, etc. — the punctuation OD removes when
# it builds the patient folder name. The PatNum-suffix fuzzy fallback in
# resolve_doc_path_with_fallback covers any residual mismatches.
_STRIP_CHARS = re.compile(r"[\W_]+", re.UNICODE)


def _od_sanitize(s: str) -> str:
    """Strip ASCII punctuation/whitespace from a name part, matching OD's
    folder-naming convention. Preserves Unicode letters."""
    if not s:
        return ""
    return _STRIP_CHARS.sub("", s)


def patient_folder_name(lname: str, fname: str, pat_num: int) -> str:
    """Return the patient folder basename: '<LName><FName><PatNum>'.

    Whitespace is trimmed from name parts AND non-alphanumeric characters are
    stripped to mirror OD's filesystem normalization. FName may be empty.
    """
    last = _od_sanitize((lname or "").strip())
    first = _od_sanitize((fname or "").strip())
    if not last:
        raise ValueError("lname must be non-empty after trimming and sanitizing")
    return f"{last}{first}{int(pat_num)}"


def parent_letter(lname: str) -> str:
    """Return the uppercased first letter of LName for the parent folder.

    Uses the first ALPHANUMERIC character — same normalization as the folder
    name, so a patient whose LName starts with a hyphen or quote gets the
    correct letter."""
    sanitized = _od_sanitize((lname or "").strip())
    if not sanitized:
        raise ValueError("lname must be non-empty after trimming and sanitizing")
    return sanitized[0].upper()


def _patient_folder_glob_match(
    parent_dir: Path,
    pat_num: int,
) -> Optional[Path]:
    """Last-resort fallback: scan a parent letter directory for a folder ending
    in the patient's PatNum. PatNum is unique across OD, so a single match is
    safe to use even if the LName/FName don't match what's in the DB.

    Returns None if zero or >1 matches.
    """
    if not parent_dir.exists() or not parent_dir.is_dir():
        return None
    suffix = str(int(pat_num))
    matches = [
        p for p in parent_dir.iterdir()
        if p.is_dir() and p.name.endswith(suffix)
        # Belt: avoid matching '15430' against '215430' by requiring the digit
        # before the suffix is non-digit (or that the folder name == suffix).
        and (len(p.name) == len(suffix) or not p.name[-len(suffix) - 1].isdigit())
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_doc_path(
    pat_num: int,
    lname: str,
    fname: str,
    file_name: str,
    *,
    share_root: Optional[Path] = None,
) -> Path:
    """Build the absolute on-disk path for a document.

    Always re-resolves from the patient's *current* LName/FName at call time —
    do not cache the resolved path. If the patient is renamed in OD, the folder
    moves and the previous path becomes stale.

    Note: this function does not check existence (callers can do `path.exists()`
    after) and does not read the file. It is pure path construction. For a
    fallback that scans the share when the constructed path doesn't exist, see
    `resolve_doc_path_with_fallback`.
    """
    if not file_name or not str(file_name).strip():
        raise ValueError("file_name must be non-empty")
    root = share_root if share_root is not None else _share_root()
    return root / parent_letter(lname) / patient_folder_name(lname, fname, pat_num) / file_name


def resolve_doc_path_with_fallback(
    pat_num: int,
    lname: str,
    fname: str,
    file_name: str,
    *,
    share_root: Optional[Path] = None,
) -> Path:
    """Like resolve_doc_path, but if the constructed path doesn't exist, scan
    the parent-letter directory for a folder ending in the patient's PatNum
    and retry there.

    Returns the constructed path even when both the primary and the fallback
    miss, so callers get a meaningful path string in the resulting "not_found"
    error message.
    """
    primary = resolve_doc_path(pat_num, lname, fname, file_name, share_root=share_root)
    if primary.exists():
        return primary
    parent = primary.parent.parent  # share/<letter>
    alt_folder = _patient_folder_glob_match(parent, pat_num)
    if alt_folder is not None:
        candidate = alt_folder / file_name
        if candidate.exists():
            return candidate
    return primary
