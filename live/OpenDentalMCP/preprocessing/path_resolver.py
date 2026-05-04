"""
Resolve a document's on-disk path under the Open Dental image share.

Open Dental stores patient documents under `<share>/<L>/<LName><FName><PatNum>/<FileName>`,
where `<L>` is the uppercased first letter of the patient's last name.

The share root is taken from the OD_DOC_ROOT environment variable, defaulting to
`\\SERVER12\OpenDentImages` (matching route_slip_ocr.py's existing behavior).

This module is read-only: it only constructs and inspects paths. It never writes,
moves, or deletes files. Tests in tests/test_safety_contract.py enforce that.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _share_root() -> Path:
    return Path(os.environ.get("OD_DOC_ROOT", r"\\SERVER12\OpenDentImages"))


def patient_folder_name(lname: str, fname: str, pat_num: int) -> str:
    """Return the patient folder basename: '<LName><FName><PatNum>'.

    Whitespace is trimmed from name parts. FName may be empty.
    """
    last = (lname or "").strip()
    first = (fname or "").strip()
    if not last:
        raise ValueError("lname must be non-empty after trimming")
    return f"{last}{first}{int(pat_num)}"


def parent_letter(lname: str) -> str:
    """Return the uppercased first letter of LName for the parent folder."""
    last = (lname or "").strip()
    if not last:
        raise ValueError("lname must be non-empty after trimming")
    return last[0].upper()


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
    after) and does not read the file. It is pure path construction.
    """
    if not file_name or not str(file_name).strip():
        raise ValueError("file_name must be non-empty")
    root = share_root if share_root is not None else _share_root()
    return root / parent_letter(lname) / patient_folder_name(lname, fname, pat_num) / file_name
