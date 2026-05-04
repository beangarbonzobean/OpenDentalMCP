"""
End-to-end intake processor.

Run as a one-shot batch (via scripts/intake_processor.py invoked from Task
Scheduler every 5-15 minutes). Each invocation:

  1. Lists *.pdf files in the watch folder.
  2. Skips any whose sha256 is already in intake_processed_pdfs.
  3. For each new PDF:
       a. Render every page to PNG (PyMuPDF) and OCR via the existing pipeline.
       b. Per-page LLM extraction (extractor.extract_page).
       c. Page split into doc candidates (page_splitter.split_pages).
       d. For each candidate:
            - Concatenate page text
            - Classify category (doc_classifier.classify_document)
            - Match patient (patient_matcher.match_patient)
            - Compute overall_confidence = min(patient, category, split)
            - If overall_confidence >= AUTO_FILE_THRESHOLD: file via filer,
              mark intake_pending row 'auto_filed'
            - Else: insert intake_pending row with status='queued'
            - Write intake_audit row for every action
       e. Mark source PDF processed (sha256 -> intake_processed_pdfs)

Pure orchestration. No destructive file ops on the watch folder. Source PDFs
stay where they were until staff (or a separate archive script) acts on them.

The processor never raises — every per-document failure is caught and logged
as an 'error' row in the cache.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from preprocessing.intake import cache as ic
from preprocessing.intake import doc_classifier
from preprocessing.intake import extractor
from preprocessing.intake import filer as filer_mod
from preprocessing.intake import page_splitter
from preprocessing.intake import patient_matcher
from preprocessing.intake.taxonomy import IntakeCategory


log = logging.getLogger(__name__)


AUTO_FILE_THRESHOLD_DEFAULT = float(
    os.environ.get("INTAKE_AUTO_FILE_THRESHOLD", "0.95")
)


@dataclass
class ProcessorResult:
    pdfs_scanned: int = 0
    pdfs_skipped_already_processed: int = 0
    pdfs_failed: int = 0
    candidates_extracted: int = 0
    candidates_auto_filed: int = 0
    candidates_queued: int = 0
    candidates_errored: int = 0
    halted_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)


def process_watch_folder(
    *,
    watch_folder: Path,
    cache_path: Optional[Path] = None,
    auto_file_threshold: Optional[float] = None,
    # Test seams / dependency injection
    ocr_pages_fn: Optional[Callable[[bytes], list[str]]] = None,
    extract_page_fn: Optional[Callable[..., extractor.PageExtraction]] = None,
    classify_fn: Optional[Callable[..., doc_classifier.ClassificationResult]] = None,
    search_patients_fn: Optional[Callable[[dict], Any]] = None,
    od_uploader_fn: Optional[Callable[[dict], Any]] = None,
    glob_pattern: str = "*.pdf",
) -> ProcessorResult:
    """Process every unprocessed PDF in `watch_folder`.

    All seams default to production implementations when None. Tests inject
    fakes for everything except cache.
    """
    watch_folder = Path(watch_folder)
    threshold = (
        auto_file_threshold
        if auto_file_threshold is not None
        else AUTO_FILE_THRESHOLD_DEFAULT
    )
    result = ProcessorResult()

    if not watch_folder.exists():
        result.halted_reason = "watch_folder_missing"
        result.errors.append(f"watch_folder {watch_folder} not found")
        return result

    cache_p = ic.init_cache(cache_path)

    # Resolve seams.
    ocr_fn = ocr_pages_fn if ocr_pages_fn is not None else _default_ocr_pages
    page_fn = extract_page_fn if extract_page_fn is not None else extractor.extract_page
    cls_fn = classify_fn if classify_fn is not None else doc_classifier.classify_document
    search_fn = search_patients_fn  # required for matching; if None, matcher gets a no-op
    upload_fn = od_uploader_fn      # required for filing; if None, auto-file paths skip

    pdfs = sorted(watch_folder.glob(glob_pattern))
    log.info("intake processor: %d PDFs in %s", len(pdfs), watch_folder)

    with ic.open_cache(cache_p) as conn:
        for pdf_path in pdfs:
            try:
                pdf_bytes = pdf_path.read_bytes()
            except Exception as e:
                result.pdfs_failed += 1
                result.errors.append(f"read_failed:{pdf_path.name}:{e}")
                continue

            sha = hashlib.sha256(pdf_bytes).hexdigest()
            if ic.is_pdf_processed(conn, sha):
                result.pdfs_skipped_already_processed += 1
                continue

            try:
                candidates_for_pdf = _process_one_pdf(
                    conn=conn,
                    pdf_path=pdf_path,
                    pdf_bytes=pdf_bytes,
                    sha=sha,
                    threshold=threshold,
                    ocr_fn=ocr_fn,
                    page_fn=page_fn,
                    cls_fn=cls_fn,
                    search_fn=search_fn,
                    upload_fn=upload_fn,
                    result=result,
                )
            except Exception as e:
                log.exception("processor: failed on %s", pdf_path)
                result.pdfs_failed += 1
                result.errors.append(f"pdf_failed:{pdf_path.name}:{e}")
                continue

            ic.mark_pdf_processed(
                conn, sha, str(pdf_path),
                page_count=candidates_for_pdf["page_count"],
                candidates=candidates_for_pdf["candidate_count"],
            )
            result.pdfs_scanned += 1

    return result


def _process_one_pdf(
    *,
    conn,
    pdf_path: Path,
    pdf_bytes: bytes,
    sha: str,
    threshold: float,
    ocr_fn: Callable[[bytes], list[str]],
    page_fn: Callable[..., extractor.PageExtraction],
    cls_fn: Callable[..., doc_classifier.ClassificationResult],
    search_fn: Optional[Callable[[dict], Any]],
    upload_fn: Optional[Callable[[dict], Any]],
    result: ProcessorResult,
) -> dict:
    """Process one PDF: OCR all pages, split, classify+match each candidate,
    write intake_pending rows, optionally auto-file."""
    page_texts = ocr_fn(pdf_bytes)  # list[str], one per page
    log.info("intake: %s -> %d pages", pdf_path.name, len(page_texts))

    extractions: list[extractor.PageExtraction] = []
    for i, ocr_text in enumerate(page_texts):
        ex = page_fn(i, ocr_text)
        extractions.append(ex)

    candidates = page_splitter.split_pages(extractions)

    for cand in candidates:
        result.candidates_extracted += 1
        text = "\n\n".join(
            page_texts[i] for i in cand.page_indices if 0 <= i < len(page_texts)
        )

        # Classify
        cls = cls_fn(text, doc_title=cand.doc_title)

        # Match patient
        if search_fn is None:
            match = patient_matcher.MatchResult(
                pat_num=None, label=None, confidence=0.0,
                reason="no_search_fn",
            )
        else:
            match = patient_matcher.match_patient(
                cand.patient_name, cand.patient_dob,
                search_patients=search_fn,
            )

        overall = min(
            cand.split_confidence,
            cls.confidence,
            match.confidence,
        )

        # Insert pending row first.
        pending = ic.IntakePending(
            source_pdf=str(pdf_path),
            source_pdf_sha256=sha,
            page_indices=list(cand.page_indices),
            extracted_name=cand.patient_name,
            extracted_dob=cand.patient_dob,
            extracted_text_len=len(text),
            suggested_pat_num=match.pat_num,
            suggested_pat_label=match.label,
            suggested_category=cls.category.short_label,
            suggested_def_num=cls.category.def_num,
            patient_confidence=match.confidence,
            category_confidence=cls.confidence,
            split_confidence=cand.split_confidence,
            overall_confidence=overall,
            status="pending",
        )
        pending_id = ic.insert_pending(conn, pending)

        ic.write_audit(conn, ic.IntakeAudit(
            pending_id=pending_id, action="extracted", actor="system",
            details={
                "source_pdf": str(pdf_path),
                "page_indices": cand.page_indices,
                "extracted_name": cand.patient_name,
                "extracted_dob": cand.patient_dob,
                "doc_title": cand.doc_title,
                "match_reason": match.reason,
                "match_candidates": match.candidates_considered,
                "category": cls.category.short_label,
                "category_confidence": cls.confidence,
                "split_confidence": cand.split_confidence,
                "overall_confidence": overall,
            },
        ))

        # Decide: auto-file or queue.
        if (
            overall >= threshold
            and match.pat_num is not None
            and upload_fn is not None
        ):
            disconnect = os.environ.get("INTAKE_DISCONNECT_OD", "false").lower() == "true"
            file_res = filer_mod.file_document(
                source_pdf_bytes=pdf_bytes,
                page_indices=cand.page_indices,
                pat_num=match.pat_num,
                def_num=cls.category.def_num,
                description=f"Auto-filed from intake (confidence={overall:.2f})",
                file_name_hint=_filename_from_candidate(cand, cls.category),
                od_uploader=upload_fn,
                disconnect=disconnect,
            )
            if file_res.success:
                final_status = "simulated_filed" if file_res.simulated else "auto_filed"
                ic.update_pending_status(
                    conn, pending_id,
                    status=final_status,
                    target_doc_num=file_res.doc_num,
                    target_file_path=file_res.file_path,
                    decided_by="auto-file",
                )
                ic.write_audit(conn, ic.IntakeAudit(
                    pending_id=pending_id, action=final_status, actor="auto-file",
                    details={
                        "doc_num": file_res.doc_num,
                        "file_path": file_res.file_path,
                        "pat_num": match.pat_num,
                        "def_num": cls.category.def_num,
                        "overall_confidence": overall,
                        "simulated": file_res.simulated,
                    },
                ))
                result.candidates_auto_filed += 1
            else:
                ic.update_pending_status(
                    conn, pending_id,
                    status="error",
                    error_message=file_res.error,
                    decided_by="auto-file",
                )
                ic.write_audit(conn, ic.IntakeAudit(
                    pending_id=pending_id, action="error", actor="auto-file",
                    details={"error": file_res.error},
                ))
                result.candidates_errored += 1
        else:
            ic.update_pending_status(conn, pending_id, status="queued")
            ic.write_audit(conn, ic.IntakeAudit(
                pending_id=pending_id, action="queued", actor="system",
                details={"overall_confidence": overall, "threshold": threshold},
            ))
            result.candidates_queued += 1

    return {
        "page_count": len(page_texts),
        "candidate_count": len(candidates),
    }


# ---------------------------------------------------------------------------
# Production OCR helper (test-injectable)
# ---------------------------------------------------------------------------

def _default_ocr_pages(pdf_bytes: bytes) -> list[str]:
    """Render the PDF to PNG pages, OCR each, and return the page texts.

    Uses the existing local-VLM (or whichever OCR_BACKEND is configured).
    Falls back to placeholders for pages that fail to OCR — so a single bad
    page doesn't drop the whole batch.
    """
    from preprocessing import ocr_helper
    from preprocessing.pdf_render import render_pdf_pages

    page_pngs = render_pdf_pages(pdf_bytes, dpi=150)
    out: list[str] = []
    for i, page_bytes in enumerate(page_pngs):
        try:
            r = ocr_helper.ocr_bytes(page_bytes, media_type="image/png")
            out.append(r.text or "")
        except Exception as e:
            log.warning("ocr_pages: page %d failed: %s", i, e)
            out.append("")
    return out


def _filename_from_candidate(cand, category: IntakeCategory) -> str:
    """Build a human-friendly filename: <category>_<patient-or-unknown>.pdf"""
    name_part = (cand.patient_name or "unknown").replace(",", "").replace(" ", "_")
    return f"{category.short_label}_{name_part}.pdf"
