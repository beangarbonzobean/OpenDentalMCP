"""
Classify a document candidate's text into one of the curated intake DocCategories.

Uses Claude Haiku with a constrained prompt — the LLM must pick one of the
short labels from `taxonomy.short_labels()`. If it returns anything else (or
fails), we fall back to MISCELLANEOUS, which always queues for staff review.

Returns a ClassificationResult with the chosen IntakeCategory and a confidence
0.0-1.0. The classifier never raises; an error path returns a result with
`error` set and the safe fallback category.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

from preprocessing.intake import taxonomy as tx
from preprocessing.intake.taxonomy import IntakeCategory


log = logging.getLogger(__name__)


CLASSIFY_MODEL_DEFAULT = os.environ.get(
    "INTAKE_CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001"
)
CLASSIFY_MAX_TOKENS = int(os.environ.get("INTAKE_CLASSIFICATION_MAX_TOKENS", "256"))


def _build_prompt(text: str, doc_title: Optional[str]) -> str:
    enum_lines = []
    for c in tx.ALL_CATEGORIES:
        enum_lines.append(f'  - "{c.short_label}": {c.description}')
    enum_block = "\n".join(enum_lines)
    title_hint = (
        f"Form title from the page header: {doc_title!r}\n\n" if doc_title else ""
    )
    return f"""\
You are classifying a scanned intake document at a dental practice.

Choose ONE category from this enum that best fits the document:
{enum_block}

{title_hint}OCR text from the document:
---
{text.strip()[:6000]}
---

Return JSON ONLY, no commentary:
{{
  "category": "<one of the short labels above>",
  "confidence": <number 0.0-1.0, your confidence in the choice>
}}

Rules:
- If the document doesn't clearly match any category, choose "miscellaneous".
- "patient_info" is for new-patient demographic forms, not medical history.
- "consent" is for treatment consents (extraction, anesthesia, etc.).
- "hipaa" is specifically the Notice of Privacy Practices, not general consents.
- "insurance_card" is a photo/scan of an insurance ID card; "insurance_eligibility"
  is a benefits printout.
- Return JSON only, no markdown fences.
"""


@dataclass
class ClassificationResult:
    category: IntakeCategory
    confidence: float
    raw_response: str = ""
    error: Optional[str] = None


def classify_document(
    text: str,
    *,
    doc_title: Optional[str] = None,
    model: str = CLASSIFY_MODEL_DEFAULT,
    max_tokens: int = CLASSIFY_MAX_TOKENS,
    caller: Optional[Callable[[str, str, int], str]] = None,
) -> ClassificationResult:
    """Classify a document's text into a curated DocCategory.

    Returns a ClassificationResult with `error=None` on success. On any failure
    (empty text, LLM error, parse failure, unknown label), returns a result
    pointing at MISCELLANEOUS with confidence=0.0 — that ensures the doc
    queues for staff review rather than being mis-filed.
    """
    if not text or not text.strip():
        return ClassificationResult(
            category=tx.MISCELLANEOUS, confidence=0.0,
            error="empty_text",
        )

    prompt = _build_prompt(text, doc_title)

    if caller is None:
        try:
            raw = _default_caller(prompt, model, max_tokens)
        except Exception as e:
            log.warning("doc classifier call failed: %s", e)
            return ClassificationResult(
                category=tx.MISCELLANEOUS, confidence=0.0,
                error=f"call_failed:{type(e).__name__}",
            )
    else:
        try:
            raw = caller(prompt, model, max_tokens)
        except Exception as e:
            return ClassificationResult(
                category=tx.MISCELLANEOUS, confidence=0.0,
                error=f"call_failed:{type(e).__name__}",
            )

    parsed = _parse_response(raw)
    label = parsed.get("category")
    cat = tx.by_short_label(label) if label else tx.MISCELLANEOUS
    conf = _clamp_conf(parsed.get("confidence"))

    if not parsed:
        return ClassificationResult(
            category=tx.MISCELLANEOUS, confidence=0.0,
            raw_response=raw, error="parse_failed",
        )

    if label not in tx.short_labels():
        # LLM returned a label outside our taxonomy. Fall back to MISC and
        # keep the response for debugging, but flag it.
        return ClassificationResult(
            category=tx.MISCELLANEOUS,
            confidence=0.0,
            raw_response=raw,
            error=f"unknown_label:{label!r}",
        )

    return ClassificationResult(
        category=cat,
        confidence=conf,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_response(raw: str) -> dict:
    if not raw:
        return {}
    s = raw.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    m = _JSON_RE.search(s)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}


def _clamp_conf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _default_caller(prompt: str, model: str, max_tokens: int) -> str:
    """Real call via the inference router — drains pre-paid Max quota first
    when there's headroom, falls through to Anthropic API when there isn't.
    Tests inject their own caller and never hit this path.

    The `model` argument is the legacy Haiku model name; it's used only as a
    fallback hint if the router ends up at the API provider. The Max-via-SDK
    path uses the user's Claude Code default model.
    """
    try:
        from inference_router import Profile, dispatch
    except ImportError:
        # Router not on this deploy — fall back to the original Anthropic call
        return _legacy_anthropic_caller(prompt, model, max_tokens)

    result = dispatch(
        Profile(
            tag="intake-classify",
            prefers_high_end=False,   # haiku-class quality is fine
            max_output_tokens=max_tokens,
        ),
        prompt,
        max_tokens=max_tokens,
        timeout=60,
    )
    log.info("intake-classify routed to %s in %d ms (cost $%.4f)",
             result.provider, result.latency_ms, result.cost_usd)
    return result.text


def _legacy_anthropic_caller(prompt: str, model: str, max_tokens: int) -> str:
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
