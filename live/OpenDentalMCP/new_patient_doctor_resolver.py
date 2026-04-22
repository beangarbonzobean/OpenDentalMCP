"""
New Patient Doctor Resolver
===========================

Resolves the examining doctor for each new patient in a date range by parsing
the patient's latest clinical (~GRP~) note and falling back to the billed
provider only when the note doesn't name a known doctor.

Why: the appointment provider and the procedure's ProvNum are frequently wrong
for our practice. Hygienist appointments, cross-scheduling, and post-visit
edits all break billing-based attribution. The truth lives in the free-text
clinical note the examining doctor wrote.

Drop-in path on the Windows server:
    C:\\Users\\Administrator\\Desktop\\Cursor\\OpenDentalMCP\\live\\OpenDentalMCP\\
        new_patient_doctor_resolver.py

Wired into mcp_tools.py (see DEPLOY.md).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Name map
# ---------------------------------------------------------------------------

# First-name (lowercase) -> provider Abbr. First-name match wins so that
# typos like "Dr Ben Y oung" or "Dr. Laura Ye" still resolve cleanly.
# Laura and Ye both map to DRYE because the note usually reads either
# "Dr. Laura Ye" or just "Dr Ye".
NOTE_NAME_TO_ABBR: Dict[str, str] = {
    "ben":    "DOCB",
    "rachel": "DOCR",
    "laura":  "DRYE",
    "ye":     "DRYE",
    "o":      "DOCO",
}


# ---------------------------------------------------------------------------
# Note parser
# ---------------------------------------------------------------------------

# Priority patterns: "Exam completed by Dr. X" > any "Dr. X".
_EXAM_PATTERNS = [
    re.compile(r"exam\s+completed\s+by\s*:?\s*dr\.?\s+([a-z]+)",  re.IGNORECASE),
    re.compile(r"exam\s*:\s*dr\.?\s+([a-z]+)",                    re.IGNORECASE),
    re.compile(r"exam\s+by\s+dr\.?\s+([a-z]+)",                   re.IGNORECASE),
]
_GENERIC_DR = re.compile(r"dr\.?\s+([a-z]+)", re.IGNORECASE)


def _name_to_abbr(token: str) -> Optional[str]:
    """Map the first word of a captured name to a provider abbreviation.

    Returns None if the name isn't one we know. Grabbing only the first
    whitespace-delimited word handles "Laura Ye" (-> Laura -> DRYE) and
    the typo "Ben Y oung" (-> Ben -> DOCB).
    """
    if not token:
        return None
    first = token.strip().lower().split()[0] if token.strip() else ""
    return NOTE_NAME_TO_ABBR.get(first)


def parse_doctor_from_note(note: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a clinical note and return {'abbr': 'DOCB', 'matched': 'Dr. Ben'}.

    Priority: explicit "Exam completed by Dr. X" attribution wins over any
    other "Dr. X" mention anywhere in the note.

    Returns None when the note is empty or names no known doctor.
    """
    if not note:
        return None

    for rx in _EXAM_PATTERNS:
        m = rx.search(note)
        if m:
            abbr = _name_to_abbr(m.group(1))
            if abbr:
                return {"abbr": abbr, "matched": m.group(0).strip()}

    for m in _GENERIC_DR.finditer(note):
        abbr = _name_to_abbr(m.group(1))
        if abbr:
            return {"abbr": abbr, "matched": m.group(0).strip()}

    return None


# ---------------------------------------------------------------------------
# SQL that feeds the resolver
# ---------------------------------------------------------------------------

# Pulls one row per new patient in the window, with:
#   - patient + address fields
#   - ApptProvider  (billed on the first-visit appointment)
#   - ExamProvider  (provider on the D0150/D0140/... procedure, ranked)
#   - NonHygProvider (first non-hygiene procedure provider, as a second fallback)
#   - ClinicalNote  (latest ~GRP~ note on the first-visit date)
#
# Note is truncated to 4000 chars via LEFT() to keep payload sane; any exam
# attribution will be in the first few hundred chars.
_SQL_TEMPLATE = """
SELECT
    p.PatNum,
    p.FName        AS FName,
    p.Preferred    AS Preferred,
    p.LName        AS LName,
    p.DateFirstVisit AS DateFirstVisit,
    CONCAT_WS(' ', p.Address, p.Address2) AS Address,
    CONCAT(p.City, ', ', p.State, ' ', p.Zip) AS CityStateZip,
    appt_prov.Abbr AS ApptProvider,
    exam_prov.Abbr AS ExamProvider,
    nonhyg_prov.Abbr AS NonHygProvider,
    LEFT(latest.Note, 4000) AS ClinicalNote
FROM patient p
INNER JOIN appointment a
    ON a.PatNum = p.PatNum
    AND DATE(a.AptDateTime) = p.DateFirstVisit
    AND a.AptStatus = 2
LEFT JOIN provider appt_prov ON a.ProvNum = appt_prov.ProvNum

/* Exam procedure, ranked by code priority */
LEFT JOIN (
    SELECT pl.PatNum, pl.ProvNum,
        ROW_NUMBER() OVER (PARTITION BY pl.PatNum ORDER BY
            CASE pc.ProcCode
                WHEN 'D0150' THEN 1 WHEN 'D0140' THEN 2 WHEN 'D0180' THEN 3
                WHEN 'D0120' THEN 4 WHEN 'D0160' THEN 5 WHEN 'D0170' THEN 6
                WHEN 'D0190' THEN 7 ELSE 99 END) AS rn
    FROM procedurelog pl
    JOIN procedurecode pc ON pc.CodeNum = pl.CodeNum
    JOIN patient p2 ON p2.PatNum = pl.PatNum
    WHERE pl.ProcStatus = 2
        AND DATE(pl.ProcDate) = p2.DateFirstVisit
        AND pc.ProcCode IN ('D0120','D0140','D0150','D0160','D0170','D0180','D0190')
) exam ON exam.PatNum = p.PatNum AND exam.rn = 1
LEFT JOIN provider exam_prov ON exam.ProvNum = exam_prov.ProvNum

/* First non-hygiene, non-radiology procedure — tertiary fallback */
LEFT JOIN (
    SELECT pl.PatNum, pl.ProvNum,
        ROW_NUMBER() OVER (PARTITION BY pl.PatNum ORDER BY pl.ProcNum) AS rn
    FROM procedurelog pl
    JOIN procedurecode pc ON pc.CodeNum = pl.CodeNum
    JOIN patient p2 ON p2.PatNum = pl.PatNum
    WHERE pl.ProcStatus = 2
        AND DATE(pl.ProcDate) = p2.DateFirstVisit
        AND pc.ProcCode NOT IN ('D1110','D1120','D1206','D1208','D4910')
        AND pc.ProcCode NOT LIKE 'D02%'
        AND pc.ProcCode NOT LIKE 'D03%'
        AND pc.ProcCode <> '~GRP~'
) nonhyg ON nonhyg.PatNum = p.PatNum AND nonhyg.rn = 1
LEFT JOIN provider nonhyg_prov ON nonhyg.ProvNum = nonhyg_prov.ProvNum

/* Latest ~GRP~ clinical note on first-visit date */
LEFT JOIN (
    SELECT pl.PatNum, pn.Note,
        ROW_NUMBER() OVER (PARTITION BY pl.PatNum ORDER BY pn.EntryDateTime DESC) AS rn
    FROM procedurelog pl
    JOIN procedurecode pc ON pc.CodeNum = pl.CodeNum
    JOIN procnote pn      ON pn.ProcNum = pl.ProcNum
    JOIN patient p3       ON p3.PatNum = pl.PatNum
    WHERE DATE(pl.ProcDate) = p3.DateFirstVisit
        AND pl.ProcStatus IN (2, 3)
        AND pc.ProcCode = '~GRP~'
) latest ON latest.PatNum = p.PatNum AND latest.rn = 1

WHERE p.DateFirstVisit BETWEEN '{from_date}' AND '{to_date}'
GROUP BY p.PatNum
ORDER BY p.DateFirstVisit ASC, p.LName ASC
"""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s.upper() if s else None


def resolve_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve one SQL row into a final doctor + source + audit info.

    Returns the original row augmented with:
        ResolvedProvider   str   — the abbreviation going on the letter
        ResolvedSource     str   — 'note' | 'exam-proc' | 'proc' | 'appt' | 'none'
        NoteMatchedText    str?  — the substring that matched, if source=='note'
        Audit              str?  — short flag explaining mismatches or gaps
    """
    parsed = parse_doctor_from_note(row.get("ClinicalNote"))

    if parsed:
        resolved = parsed["abbr"]
        source   = "note"
        matched  = parsed["matched"]
    elif _norm(row.get("ExamProvider")):
        resolved = _norm(row.get("ExamProvider"))
        source   = "exam-proc"
        matched  = None
    elif _norm(row.get("NonHygProvider")):
        resolved = _norm(row.get("NonHygProvider"))
        source   = "proc"
        matched  = None
    elif _norm(row.get("ApptProvider")):
        resolved = _norm(row.get("ApptProvider"))
        source   = "appt"
        matched  = None
    else:
        resolved = ""
        source   = "none"
        matched  = None

    # Audit flags
    audit = None
    note_present = bool((row.get("ClinicalNote") or "").strip())
    if source == "note":
        # Note named a doctor; compare against the billed provider for visibility.
        billed = _norm(row.get("ExamProvider")) or _norm(row.get("ApptProvider"))
        if billed and billed != resolved:
            audit = f"Note doctor ({resolved}) != billed ({billed})"
    else:
        if not note_present:
            audit = "No clinical note — used billing fallback"
        else:
            audit = "Note did not name a mappable doctor — used billing fallback"

    out = dict(row)
    out["ResolvedProvider"] = resolved
    out["ResolvedSource"]   = source
    out["NoteMatchedText"]  = matched
    out["Audit"]            = audit
    return out


# ---------------------------------------------------------------------------
# Tool entrypoint
# ---------------------------------------------------------------------------

def get_new_patient_exam_doctors(
    tools,
    from_date: str,
    to_date: str,
    include_note_text: bool = False,
) -> List[Dict[str, Any]]:
    """Run the resolver for a date range and return one row per new patient.

    Args:
        tools: OpenDentalMCPTools instance (needed for _query_database).
        from_date: inclusive start YYYY-MM-DD
        to_date:   inclusive end   YYYY-MM-DD
        include_note_text: if True, keep the raw ClinicalNote in each row
                           (useful for debugging / audit). Defaults False so
                           responses stay small.

    Returns:
        List of dicts. Each dict is one new patient, with keys:
            PatNum, FName, Preferred, LName, DateFirstVisit,
            Address, CityStateZip,
            ApptProvider, ExamProvider, NonHygProvider,
            ResolvedProvider, ResolvedSource, NoteMatchedText, Audit.
            (ClinicalNote is included only when include_note_text=True.)
    """
    _validate_date(from_date, "from_date")
    _validate_date(to_date,   "to_date")

    sql = _SQL_TEMPLATE.format(from_date=from_date, to_date=to_date)

    # Route through the existing _query_database path. That method opens a
    # direct pyodbc/pymysql connection, executes the SQL, ISO-stringifies
    # datetimes, and returns row-dicts — exactly the shape resolve_row wants.
    # This matches the convention used by every other SQL-driven tool in
    # mcp_tools.py.
    result = tools._query_database(sql, limit=10000)
    if not result.get("success"):
        raise Exception(f"Resolver SQL failed: {result.get('error')}")
    rows = result.get("rows", [])

    resolved = [resolve_row(r) for r in rows]

    if not include_note_text:
        for r in resolved:
            r.pop("ClinicalNote", None)

    return resolved


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _validate_date(v: str, name: str) -> None:
    if not isinstance(v, str) or not _DATE_RE.match(v):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {v!r}")


# ---------------------------------------------------------------------------
# Tool schema (for list_tools())
# ---------------------------------------------------------------------------

TOOL_SCHEMA = {
    "name": "get_new_patient_exam_doctors",
    "description": (
        "For each new patient whose first-visit date falls in the given "
        "range, resolve which doctor actually examined them by parsing the "
        "latest clinical (~GRP~) note. Falls back to exam-procedure, "
        "non-hygiene-procedure, then appointment provider when the note is "
        "missing or doesn't name a known doctor. "
        "Use this to drive the new-patient thank-you letter pipeline."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "from_date": {
                "type": "string",
                "description": "Inclusive start date, YYYY-MM-DD.",
            },
            "to_date": {
                "type": "string",
                "description": "Inclusive end date, YYYY-MM-DD.",
            },
            "include_note_text": {
                "type": "boolean",
                "description": (
                    "Include the raw clinical-note text in each row for "
                    "auditing. Defaults to false to keep responses small."
                ),
                "default": False,
            },
        },
        "required": ["from_date", "to_date"],
    },
}
