"""
Replay the intake extractor + matcher against the cached OCR text for each
candidate in a batch and compare to the original audit outcome. Used to
verify that an extractor or matcher change actually fixes the rejected cases
*without* re-running the full OCR pipeline (which costs ~10min and depends
on local-VLM availability).

Usage:
    python scripts/replay_intake_rejections.py <sha256-prefix>

Reads:
  - data/intake.db                  -> intake_pending + intake_audit
  - data/document_text_cache.db     -> per-page OCR text from intake_daytime
  - OD via mcp_tools._search_patients (real network call to the live MCP)

Writes nothing. Prints a side-by-side diff for each candidate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_DIR))

INTAKE_DB = _PKG_DIR / "data" / "intake.db"
CACHE_DB = _PKG_DIR / "data" / "document_text_cache.db"


def _ocr_pages_for(sha: str, page_indices: list[int]) -> list[str]:
    """Return one string per page in order. Empty list if not cached."""
    if not CACHE_DB.exists():
        return []
    con = sqlite3.connect(str(CACHE_DB))
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(page_indices))
    rows = con.execute(
        f"SELECT PageIndex, Text FROM doc_text "
        f"WHERE Source='intake_daytime' AND Sha256=? AND PageIndex IN ({placeholders}) "
        f"ORDER BY PageIndex",
        (sha, *page_indices),
    ).fetchall()
    con.close()
    return [(r["Text"] or "") for r in rows]


def _final_action(audit_rows: list) -> tuple[str, str]:
    for r in reversed(audit_rows):
        if r["action"] in ("extracted", "queued"):
            continue
        details = {}
        try:
            details = json.loads(r["details"] or "{}")
        except Exception:
            pass
        reason = details.get("reason") or ""
        return r["action"], reason
    return "no_decision", ""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: replay_intake_rejections.py <sha256-prefix>", file=sys.stderr)
        return 2
    sha_prefix = args[0]

    from preprocessing.intake import extractor, patient_matcher
    import mcp_tools

    tools = mcp_tools.OpenDentalMCPTools()

    con = sqlite3.connect(str(INTAKE_DB))
    con.row_factory = sqlite3.Row
    sha_full = con.execute(
        "SELECT source_pdf_sha256 FROM intake_pending "
        "WHERE source_pdf_sha256 LIKE ? LIMIT 1",
        (sha_prefix + "%",),
    ).fetchone()
    if not sha_full:
        print(f"no intake_pending rows for sha prefix {sha_prefix!r}", file=sys.stderr)
        return 2
    sha_full = sha_full[0]

    rows = con.execute(
        "SELECT * FROM intake_pending WHERE source_pdf_sha256=? ORDER BY id",
        (sha_full,),
    ).fetchall()
    print(f"Replaying {len(rows)} candidates from sha={sha_full[:12]}\n")

    n_better = 0
    n_same = 0
    n_worse = 0

    for r in rows:
        pid = int(r["id"])
        page_indices = json.loads(r["page_indices"] or "[]")
        audit = con.execute(
            "SELECT * FROM intake_audit WHERE pending_id=? ORDER BY id",
            (pid,),
        ).fetchall()
        action, reason = _final_action(audit)

        # `--all` mode replays every candidate; default replays only rejects.
        if "--all" not in (sys.argv or []) and action != "rejected":
            continue

        page_texts = _ocr_pages_for(sha_full, page_indices)
        if not page_texts:
            print(f"[id {pid}] pp{page_indices} — no cached OCR text, skipping")
            continue

        # Production code calls extract_page once per page, then page_splitter
        # picks the first non-null name across the candidate's pages. For the
        # replay we mimic that: extract per-page, then take the first page
        # whose extractor returned a real name.
        new_ext = None
        for i, text in enumerate(page_texts):
            ext = extractor.extract_page(i, text)
            if ext.patient_name:
                new_ext = ext
                break
        if new_ext is None:
            new_ext = extractor.extract_page(0, page_texts[0])
        new_extracted_name = new_ext.patient_name or ""

        # Re-run the matcher with the (NEW) extracted name.
        match = patient_matcher.match_patient(
            new_extracted_name, new_ext.patient_dob,
            search_patients=tools._search_patients,
        )

        old_name = r["extracted_name"] or ""
        old_label = r["suggested_pat_label"] or "—"
        old_conf = r["patient_confidence"] or 0
        new_label = match.label or "—"
        new_conf = match.confidence

        # Verdict relative to staff truth (which we know rejected this case)
        verdict = "?"
        if new_label == old_label and abs(new_conf - old_conf) < 0.01:
            verdict = "SAME (still wrong)"
            n_same += 1
        elif new_label == "—":
            verdict = "BETTER (now unmatched, no false confident match)"
            n_better += 1
        elif new_conf < old_conf:
            verdict = f"BETTER (lower conf {old_conf:.2f}->{new_conf:.2f})"
            n_better += 1
        elif new_conf > old_conf:
            verdict = f"WORSE (higher conf {old_conf:.2f}->{new_conf:.2f})"
            n_worse += 1
        else:
            verdict = f"DIFFERENT ({old_label!r} -> {new_label!r})"
            n_better += 1  # any change away from a known-wrong is improvement

        print(f"--- id {pid} pp{page_indices} ---")
        print(f"  staff reject reason: {reason!r}")
        print(f"  OLD extracted_name: {old_name!r}")
        print(f"  NEW extracted_name: {new_extracted_name!r}")
        print(f"  OLD match: {old_label} conf={old_conf:.2f}")
        print(f"  NEW match: {new_label} conf={new_conf:.2f} reason={match.reason}")
        print(f"  -> {verdict}")
        print()

    print(f"Summary: better={n_better} same={n_same} worse={n_worse}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
