"""
Replay the intake page splitter on a source PDF and produce a Markdown report
that compares the current splitter's decisions against an alternate rule and
against any staff outcomes recorded in intake_pending / intake_audit.

Why this exists: when a 13-page route-slip batch on 2026-05-04 had two
consecutive patients merged into one candidate, the only signal we had to
investigate was the rejection note ("two patient charts in one"). This script
gives us a structured way to (a) reproduce the splitter's reasoning per page
boundary, (b) try a candidate fix, and (c) check whether the alternate would
have agreed with staff on past batches.

Usage:
    python scripts/splitter_eval.py "<path-to-pdf>" [--output <md-path>]
    python scripts/splitter_eval.py --sha256 <hex>          # look up by sha
    python scripts/splitter_eval.py --pdf <path> --no-llm   # skip re-extraction;
                                                              read intake_pending.

Reads OCR_BACKEND / LOCAL_VLM_* / ANTHROPIC_API_KEY env vars from the process.
Set MCP_CONFIG_FILE=config.prod.json before invocation.

Read-only against intake.db. Re-running OCR will populate document_text_cache
as a side effect (since _default_ocr_pages now writes there).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make live/OpenDentalMCP importable when launched as a script.
_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))


log = logging.getLogger("splitter_eval")


# ---------------------------------------------------------------------------
# Alternate splitter rule
# ---------------------------------------------------------------------------

# The current rule (preprocessing.intake.page_splitter._decide_split) treats
# is_continuation as the highest-priority signal: if the per-page extractor
# returns is_continuation=True, no split happens regardless of name change.
#
# That's what merged RUBARTH (page 11) with CABRERA (page 12) on 2026-05-04 —
# the extractor saw the same template and labelled page 12 as a continuation
# even though the patient name in the upper-left changed.
#
# Header-priority rule: if BOTH a real running name and a real (different)
# page name exist, the name change wins, even if is_continuation=True. We
# only trust is_continuation when the page either has no name or the name
# matches the running doc.

def alt_split_pages(extractions):
    """Header-priority variant of page_splitter.split_pages."""
    from preprocessing.intake import page_splitter as ps

    pages = list(extractions)
    if not pages:
        return []

    candidates = []
    current_pages: list = []
    running_name: Optional[str] = None
    running_title: Optional[str] = None
    split_signals: list[float] = []

    def flush():
        if current_pages:
            candidates.append(ps._build_candidate(current_pages, split_signals))

    for i, page in enumerate(pages):
        is_first = i == 0
        starts_new, signal = _alt_decide_split(
            page=page, running_name=running_name,
            running_title=running_title, is_first=is_first,
        )
        if starts_new and current_pages:
            flush()
            current_pages = []
            running_name = None
            running_title = None
            split_signals = []
        current_pages.append(page)
        split_signals.append(signal)
        if running_name is None and ps._is_real_name(page.patient_name):
            running_name = page.patient_name
        if running_title is None and ps._is_real_title(page.doc_title):
            running_title = page.doc_title

    flush()
    return candidates


def _alt_decide_split(*, page, running_name, running_title, is_first):
    from preprocessing.intake import page_splitter as ps

    if is_first:
        return True, 1.0

    # Header-priority: strong name change wins even over is_continuation.
    name_clearly_changed = (
        running_name is not None
        and ps._is_real_name(page.patient_name)
        and not ps._names_match(running_name, page.patient_name)
    )
    if name_clearly_changed:
        return True, 1.0

    if page.is_continuation:
        return False, 0.95

    if page.error or (page.patient_name is None and page.doc_title is None):
        return False, 0.6

    if running_title and ps._is_real_title(page.doc_title) and not ps._titles_match(
        running_title, page.doc_title
    ):
        return True, 0.85

    return False, 0.8


# ---------------------------------------------------------------------------
# Intake DB joins
# ---------------------------------------------------------------------------

_INTAKE_DB_OVERRIDE: Optional[Path] = None


def _intake_db_path() -> Path:
    if _INTAKE_DB_OVERRIDE is not None:
        return _INTAKE_DB_OVERRIDE
    return _PKG_DIR / "data" / "intake.db"


def load_pending_for_sha(sha: str):
    """Return list of (pending_row, audit_rows) ordered by pending_id."""
    db = _intake_db_path()
    if not db.exists():
        log.warning("intake.db not found at %s — staff feedback section will be empty", db)
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM intake_pending WHERE source_pdf_sha256 = ? ORDER BY id",
            (sha,),
        ).fetchall()
        out = []
        for r in rows:
            audit = conn.execute(
                "SELECT * FROM intake_audit WHERE pending_id = ? ORDER BY id",
                (int(r["id"]),),
            ).fetchall()
            out.append((dict(r), [dict(a) for a in audit]))
        return out
    finally:
        conn.close()


def _final_status(audit_rows: list) -> tuple[str, Optional[str]]:
    """Return (final_action, reason) from the latest non-extracted/queued audit."""
    for r in reversed(audit_rows):
        if r["action"] in ("extracted", "queued"):
            continue
        try:
            details = json.loads(r["details"] or "{}")
        except Exception:
            details = {}
        return r["action"], details.get("reason")
    return audit_rows[-1]["action"] if audit_rows else "unknown", None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    pdf_path: Path
    sha256: str
    page_count: int
    extractions: list = field(default_factory=list)
    original_candidates: list = field(default_factory=list)
    alternate_candidates: list = field(default_factory=list)
    pending: list = field(default_factory=list)


def run(pdf_path: Path) -> EvalResult:
    from preprocessing.intake import extractor as ex
    from preprocessing.intake import page_splitter as ps
    from preprocessing.intake import processor as proc

    pdf_bytes = pdf_path.read_bytes()
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    log.info("eval: %s sha=%s", pdf_path.name, sha[:12])

    # OCR every page (this also writes to document_text_cache as a side effect).
    page_texts = proc._default_ocr_pages(
        pdf_bytes,
        source_pdf_sha256=sha,
        source_pdf_path=str(pdf_path),
    )
    log.info("eval: %d pages OCR'd", len(page_texts))

    extractions = []
    for i, text in enumerate(page_texts):
        try:
            extractions.append(ex.extract_page(i, text))
        except Exception as e:  # pragma: no cover - defensive
            log.warning("eval: extract failed page %d: %s", i, e)
            extractions.append(ex.PageExtraction(
                page_idx=i, patient_name=None, patient_dob=None,
                doc_title=None, is_continuation=False,
                error=f"{type(e).__name__}: {e}",
            ))

    original = ps.split_pages(extractions)
    alternate = alt_split_pages(extractions)
    pending = load_pending_for_sha(sha)

    return EvalResult(
        pdf_path=pdf_path,
        sha256=sha,
        page_count=len(page_texts),
        extractions=extractions,
        original_candidates=original,
        alternate_candidates=alternate,
        pending=pending,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _md_extractions_table(extractions) -> str:
    lines = [
        "| Page | Extracted name | DOB | Doc title | is_continuation | Error |",
        "| ---: | --- | --- | --- | :---: | --- |",
    ]
    for e in extractions:
        lines.append(
            f"| {e.page_idx + 1} "
            f"| {e.patient_name or '—'} "
            f"| {e.patient_dob or '—'} "
            f"| {(e.doc_title or '—')[:40]} "
            f"| {'YES' if e.is_continuation else '·'} "
            f"| {(e.error or '')[:40]} |"
        )
    return "\n".join(lines)


def _md_candidates_block(label: str, candidates) -> str:
    lines = [f"### {label} — {len(candidates)} candidate(s)"]
    for i, c in enumerate(candidates, 1):
        pages = ",".join(str(p + 1) for p in c.page_indices)
        lines.append(
            f"- **Cand {i}**: pages [{pages}] · name=`{c.patient_name or '—'}` "
            f"· title=`{(c.doc_title or '—')[:30]}` "
            f"· split_conf={c.split_confidence}"
        )
    return "\n".join(lines)


def _md_diff_block(original, alternate) -> str:
    """Identify boundaries where the two splitters disagree."""
    def _starts(cands):
        # Set of page indices that start a new candidate.
        return {c.page_indices[0] for c in cands if c.page_indices}

    o = _starts(original)
    a = _starts(alternate)
    extra_alt = sorted(a - o)
    extra_orig = sorted(o - a)

    lines = ["### Splitter disagreements"]
    if not extra_alt and not extra_orig:
        lines.append("- Original and alternate agree on every boundary.")
        return "\n".join(lines)

    if extra_alt:
        lines.append(
            "- **Alternate splits HERE; original does not** at page "
            + ", ".join(str(p + 1) for p in extra_alt)
            + " — alternate would have created an extra candidate."
        )
    if extra_orig:
        lines.append(
            "- **Original splits HERE; alternate does not** at page "
            + ", ".join(str(p + 1) for p in extra_orig)
            + " — alternate would have merged into the previous candidate."
        )
    return "\n".join(lines)


def _md_staff_block(pending) -> str:
    if not pending:
        return "### Staff feedback\n\n- No intake_pending rows found for this sha256."
    lines = ["### Staff feedback (intake_pending + intake_audit)"]
    lines.append("")
    lines.append("| pending_id | pages | suggested patient | category | status | reason |")
    lines.append("| ---: | --- | --- | --- | --- | --- |")
    for r, audit in pending:
        try:
            page_idxs = json.loads(r.get("page_indices") or "[]")
        except Exception:
            page_idxs = []
        pages = ",".join(str(p + 1) for p in page_idxs) or "—"
        action, reason = _final_status(audit)
        lines.append(
            f"| {r['id']} | {pages} | {r.get('suggested_pat_label') or '—'} "
            f"| {r.get('suggested_category') or '—'} | {action} "
            f"| {(reason or '')[:60]} |"
        )
    return "\n".join(lines)


def _md_alt_vs_staff(alt_candidates, pending) -> str:
    """For each rejected pending row, ask whether the alternate would have
    produced the same merged candidate or split it."""
    lines = ["### Alternate vs. staff outcomes"]
    rejected = [
        (r, audit) for (r, audit) in pending
        if any(a["action"] == "rejected" for a in audit)
    ]
    if not rejected:
        lines.append("- No rejected rows in this batch.")
        return "\n".join(lines)

    alt_starts = {c.page_indices[0] for c in alt_candidates if c.page_indices}
    for r, audit in rejected:
        try:
            page_idxs = json.loads(r.get("page_indices") or "[]")
        except Exception:
            page_idxs = []
        if not page_idxs or len(page_idxs) < 2:
            continue
        # Did the alternate split anywhere within this multi-page candidate?
        internal = [p for p in page_idxs[1:] if p in alt_starts]
        action, reason = _final_status(audit)
        if internal:
            verdict = (
                f"**Alternate would split** at page(s) "
                + ", ".join(str(p + 1) for p in internal)
                + " ✓ matches staff rejection"
            )
        else:
            verdict = "Alternate would also have merged ✗ (no improvement)"
        lines.append(
            f"- pending_id {r['id']} (pages "
            + ",".join(str(p + 1) for p in page_idxs)
            + f"), staff: {action} ({reason or 'no reason'}). {verdict}"
        )
    return "\n".join(lines)


def to_markdown(result: EvalResult) -> str:
    out = []
    out.append(f"# Splitter eval — {result.pdf_path.name}")
    out.append("")
    out.append(f"- Path: `{result.pdf_path}`")
    out.append(f"- SHA256: `{result.sha256}`")
    out.append(f"- Page count: {result.page_count}")
    out.append(f"- Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}")
    out.append("")
    out.append("## Per-page extraction")
    out.append("")
    out.append(_md_extractions_table(result.extractions))
    out.append("")
    out.append("## Splitter results")
    out.append("")
    out.append(_md_candidates_block("Original (current production rule)",
                                       result.original_candidates))
    out.append("")
    out.append(_md_candidates_block("Alternate (header-priority rule)",
                                       result.alternate_candidates))
    out.append("")
    out.append(_md_diff_block(result.original_candidates, result.alternate_candidates))
    out.append("")
    out.append("## Cross-check vs. staff")
    out.append("")
    out.append(_md_staff_block(result.pending))
    out.append("")
    out.append(_md_alt_vs_staff(result.alternate_candidates, result.pending))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pdf", nargs="?", type=Path, help="Path to source PDF")
    p.add_argument("--output", type=Path, default=None,
                   help="Optional MD output path. If omitted, prints to stdout.")
    p.add_argument("--intake-db", type=Path, default=None,
                   help="Override path to intake.db (defaults to ../data/intake.db).")
    p.add_argument("--cache-path", type=Path, default=None,
                   help="Override path to document_text_cache.db so OCR side-effects "
                        "land in the production cache instead of the worktree's.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    global _INTAKE_DB_OVERRIDE
    if args.intake_db:
        _INTAKE_DB_OVERRIDE = args.intake_db.resolve()
    if args.cache_path:
        # Point document_text_cache.DEFAULT_CACHE_PATH at the override so all
        # subsequent init_cache() / put_intake_page_text() calls land there.
        from preprocessing import document_text_cache as dtc
        dtc.DEFAULT_CACHE_PATH = args.cache_path.resolve()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.pdf is None:
        print("Provide a PDF path.", file=sys.stderr)
        return 2
    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    result = run(args.pdf)
    md = to_markdown(result)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
