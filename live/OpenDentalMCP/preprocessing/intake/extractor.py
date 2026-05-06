"""
Per-page LLM extraction for intake batch scans.

Given an OCR'd page (text), return a structured JSON record with:
  - patient_name: best-effort extraction from the page (str | None)
  - patient_dob: ISO YYYY-MM-DD, best-effort (str | None)
  - doc_title: the form title at the top of the page if any (str | None)
  - is_continuation: bool — does this page look like a continuation of the
    previous page rather than the start of a new doc?

Used by page_splitter to decide where one document ends and the next begins.

We use Claude Haiku via the existing ocr_helper Anthropic client (no new
dependency). Haiku is more reliable than the local VLM for structured-output
tasks and the per-page cost is ~$0.001-0.003 — small change vs. the value
of correct splits.

Test seam: pass `caller=` to inject a fake LLM response.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional


log = logging.getLogger(__name__)


EXTRACTION_MODEL_DEFAULT = os.environ.get(
    "INTAKE_EXTRACTION_MODEL", "claude-haiku-4-5-20251001"
)
EXTRACTION_MAX_TOKENS = int(os.environ.get("INTAKE_EXTRACTION_MAX_TOKENS", "1024"))


_PROMPT = """\
You are processing a scanned page from a dental practice's end-of-day intake batch.
Each page is one of: a patient intake form, medical history, consent form, HIPAA
notice, insurance card photo, eligibility printout, referral letter, or other.

Given the OCR text below, extract structured JSON ONLY. Do not add commentary.

Schema:
{
  "patient_name": "Lastname, Firstname" | null,    // Combined into one string. Use whatever case the form used. null if no clear name.
  "patient_dob": "YYYY-MM-DD" | null,              // Reformat any date you find. null if not present.
  "doc_title": "the visible header at the top of the page" | null,
  "is_continuation": true | false                  // True if this page looks like a continuation of a multi-page form (e.g., page 2 of 2, no header repeated, mid-form fields).
}

Rules:
- The PATIENT NAME is the most prominent name in the upper-left header
  area of the page, usually printed in large/bold text right under the
  form title (e.g., "Routing Slip" or "Consent for Dental Treatment").
- IGNORE STAFF NAMES. Lines that label staff are NOT the patient. Treat
  these as staff and skip them, even if a name follows on the same line:
    * "DOC-", "DOCB-", "DOCA-" (doctor)
    * "GEORG-", "SOPH-", "TUYN-", "VO-", "ASSIST-", "HYG-" (hygienist or
      assistant; the prefix is the first 4-6 letters of their first name)
    * "Dr. Name", "Provider:", "Hygienist:", "Assistant:"
  Example: a line "DOCB- YOUNG, BEN" identifies Dr. Ben Young — that is
  the doctor, not the patient. The patient name will be elsewhere on the
  page (typically the bold header at the top).
- FALLBACK: if the OCR didn't pick up the prominent header name (it can
  fail on bold or stylized headers), look for "Subscriber:" under
  Primary/Secondary Insurance — on adult patients the subscriber is
  typically the patient. Only use this fallback when no other patient-
  shaped name is visible in the page body. Note: for minors, the
  subscriber is usually a parent — if Age looks under 18, prefer null
  over an insurance-subscriber name unless the subscriber name clearly
  matches a name elsewhere in the body.
- If the page shows multiple names (e.g., subscriber + dependent on insurance card),
  pick the patient (the one being treated) — usually the dependent if names differ.
- DOB formats vary: 04/12/1980, 4-12-80, April 12 1980. Convert all to YYYY-MM-DD.
  Two-digit years: assume 19XX if XX > current_year_short else 20XX.
- doc_title is the form title at the top, like "PATIENT INFORMATION FORM",
  "MEDICAL HISTORY", "CONSENT FOR EXTRACTION", not random text.
- is_continuation=true if there's no clear form header AND the visible content
  looks like the middle of a longer form (continuing checklists, signature
  blocks, page-2 indicators).

Return JSON only, no preamble.
"""


@dataclass
class PageExtraction:
    page_idx: int
    patient_name: Optional[str]
    patient_dob: Optional[str]
    doc_title: Optional[str]
    is_continuation: bool
    raw_response: str = ""  # for debugging
    error: Optional[str] = None


def extract_page(
    page_idx: int,
    ocr_text: str,
    *,
    model: str = EXTRACTION_MODEL_DEFAULT,
    max_tokens: int = EXTRACTION_MAX_TOKENS,
    caller: Optional[Callable[[str, str, int], str]] = None,
) -> PageExtraction:
    """Extract structured fields from one page's OCR text.

    `caller(prompt, model, max_tokens) -> raw_text` is the test seam. If None,
    a Claude Haiku call is made via the Anthropic SDK.

    Always returns a PageExtraction. On any error, returns one with `error`
    set and the other fields None / False — never raises. The pipeline
    continues; the page is treated as low-confidence.
    """
    if not ocr_text or not ocr_text.strip():
        return PageExtraction(
            page_idx=page_idx,
            patient_name=None, patient_dob=None,
            doc_title=None, is_continuation=False,
            error="empty_ocr_text",
        )

    user_text = f"{_PROMPT}\n\n--- OCR TEXT ---\n{ocr_text.strip()[:6000]}"

    if caller is None:
        try:
            raw = _default_caller(user_text, model, max_tokens)
        except Exception as e:
            log.warning("page %d extraction call failed: %s", page_idx, e)
            return PageExtraction(
                page_idx=page_idx,
                patient_name=None, patient_dob=None,
                doc_title=None, is_continuation=False,
                error=f"call_failed:{type(e).__name__}",
            )
    else:
        try:
            raw = caller(user_text, model, max_tokens)
        except Exception as e:
            return PageExtraction(
                page_idx=page_idx,
                patient_name=None, patient_dob=None,
                doc_title=None, is_continuation=False,
                error=f"call_failed:{type(e).__name__}",
            )

    parsed = _parse_response(raw)
    return PageExtraction(
        page_idx=page_idx,
        patient_name=parsed.get("patient_name"),
        patient_dob=_normalize_dob(parsed.get("patient_dob")),
        doc_title=parsed.get("doc_title"),
        is_continuation=bool(parsed.get("is_continuation", False)),
        raw_response=raw,
        error=None if parsed else "parse_failed",
    )


def _default_caller(prompt: str, model: str, max_tokens: int) -> str:
    """Real Anthropic call. Tests inject their own caller."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content)


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_response(raw: str) -> dict:
    """Pull the first JSON object out of the LLM response."""
    if not raw:
        return {}
    raw = raw.strip()
    # If the model returned just JSON, fast path.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    # Otherwise, find the first {...} block.
    m = _JSON_RE.search(raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "mdy"),
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), "mdy"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$"), "mdy_short"),
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2})$"), "mdy_short"),
]


def _normalize_dob(dob: Optional[str]) -> Optional[str]:
    """Best-effort normalize the DOB to YYYY-MM-DD. Returns None if it can't be parsed."""
    if not dob:
        return None
    s = str(dob).strip()
    for pat, kind in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif kind == "mdy":
                mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:  # mdy_short
                mo, d, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                # Two-digit year: 00-29 -> 2000s, 30-99 -> 1900s.
                y = 2000 + yy if yy < 30 else 1900 + yy
            if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
                return None
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            continue
    return None
