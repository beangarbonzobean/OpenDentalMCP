"""
Claude vision wrapper for OCR'ing dental practice documents.

Self-contained — does not depend on the untracked route_slip_ocr.py. The two
should be unified later by porting route_slip_ocr.py to call this module.

Returns a structured OcrResult so callers know whether the API ran, what model
ran, and a rough cost estimate (used by the budget guardrail in
document_text_index.backfill).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost model — Haiku 4.5 (claude-haiku-4-5-20251001) public pricing
# ---------------------------------------------------------------------------
# Last verified: 2025-10. Update if prices change.
# Input:  $1 / 1M tokens
# Output: $5 / 1M tokens
# Image tokenization is approximate; we use a conservative per-MP estimate.
HAIKU_INPUT_USD_PER_TOKEN = 1.00 / 1_000_000
HAIKU_OUTPUT_USD_PER_TOKEN = 5.00 / 1_000_000

DEFAULT_MODEL = os.environ.get("PREPROC_OCR_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_MAX_TOKENS = int(os.environ.get("PREPROC_OCR_MAX_TOKENS", "4096"))


GENERIC_OCR_PROMPT = (
    "Transcribe all printed and handwritten text from this dental practice document. "
    "Preserve structure (headings, fields, tables) using plain text. "
    "Do not summarize, paraphrase, or add commentary. "
    "If the document is illegible, blank, or contains no text, respond with the single word UNREADABLE "
    "and nothing else."
)


@dataclass
class OcrResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    media_type: str
    is_unreadable: bool


class OcrError(RuntimeError):
    """Raised when the OCR call fails for any reason that should be retried
    or marked as a per-document error."""


class OcrRateLimited(OcrError):
    """The API returned 429. Caller should back off and retry."""


class OcrConfigError(OcrError):
    """The OCR helper cannot proceed (missing API key, etc.). Not retryable."""


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * HAIKU_INPUT_USD_PER_TOKEN
        + output_tokens * HAIKU_OUTPUT_USD_PER_TOKEN
    )


def ocr_bytes(
    file_bytes: bytes,
    *,
    media_type: str,
    prompt: str = GENERIC_OCR_PROMPT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = 3,
    initial_backoff_seconds: float = 2.0,
    client_factory=None,
) -> OcrResult:
    """Send an image or PDF to Claude vision and return the transcribed text.

    media_type must be one of: image/jpeg, image/png, image/gif, image/webp, application/pdf.

    PDFs use a `document` content block; images use an `image` content block.

    Retries on 429 with exponential backoff up to max_retries. On 5xx, retries
    once with a short delay. Other errors raise OcrError.

    `client_factory` is for tests — a no-arg callable returning an Anthropic
    client. Production code passes None and the helper builds its own.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise OcrConfigError("ANTHROPIC_API_KEY not set")

    if client_factory is None:
        from anthropic import Anthropic
        def client_factory():
            return Anthropic(api_key=api_key)

    client = client_factory()
    b64 = base64.standard_b64encode(file_bytes).decode("ascii")

    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
    elif media_type.startswith("image/"):
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
    else:
        raise OcrError(f"Unsupported media_type: {media_type}")

    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{
                    "role": "user",
                    "content": [content_block, {"type": "text", "text": prompt}],
                }],
            )
        except Exception as e:  # anthropic.APIError, RateLimitError, etc.
            last_err = e
            status = _status_from_error(e)
            if status == 429 and attempt + 1 < max_retries:
                delay = initial_backoff_seconds * (2 ** attempt)
                log.warning("OCR 429, backing off %.1fs (attempt %d)", delay, attempt + 1)
                time.sleep(delay)
                continue
            if status is not None and 500 <= status < 600 and attempt + 1 < max_retries:
                delay = initial_backoff_seconds
                log.warning("OCR %d, retrying after %.1fs", status, delay)
                time.sleep(delay)
                continue
            if status == 429:
                raise OcrRateLimited(str(e)) from e
            raise OcrError(str(e)) from e
        else:
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            usage = getattr(msg, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            cost = _estimate_cost(input_tokens, output_tokens)
            is_unreadable = text.upper().strip() == "UNREADABLE"
            return OcrResult(
                text="" if is_unreadable else text,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                media_type=media_type,
                is_unreadable=is_unreadable,
            )

    # Loop exhausted without returning.
    raise OcrError(f"OCR failed after {max_retries} attempts: {last_err!r}")


def _status_from_error(err: Exception) -> Optional[int]:
    """Extract HTTP status from various Anthropic error shapes; None if unknown."""
    for attr in ("status_code", "status"):
        v = getattr(err, attr, None)
        if isinstance(v, int):
            return v
    response = getattr(err, "response", None)
    if response is not None:
        v = getattr(response, "status_code", None)
        if isinstance(v, int):
            return v
    return None


# ---------------------------------------------------------------------------
# Media-type classification
# ---------------------------------------------------------------------------

# Extensions that we OCR with vision. Lowercased keys.
_IMAGE_EXTS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",  # TIFF support varies; treated as image but may fail in API
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
_PDF_EXTS = {".pdf": "application/pdf"}

# Extensions we explicitly skip — radiograph / DICOM / video / archives.
_UNSUPPORTED_EXTS = {
    ".dcm", ".dxr", ".dxg", ".dxx",  # DICOM / Dexis x-ray formats
    ".mp4", ".mov", ".avi", ".webm",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".exe", ".dll",
}


def classify_extension(file_name: str) -> tuple[str, Optional[str]]:
    """Return ('image' | 'pdf' | 'unsupported', media_type | None).

    Unknown extensions are treated as 'unsupported' to avoid sending random
    files to Claude. Add extensions to _IMAGE_EXTS / _PDF_EXTS as needed.
    """
    if not file_name:
        return ("unsupported", None)
    ext = ("." + file_name.rsplit(".", 1)[-1]).lower() if "." in file_name else ""
    if ext in _IMAGE_EXTS:
        return ("image", _IMAGE_EXTS[ext])
    if ext in _PDF_EXTS:
        return ("pdf", _PDF_EXTS[ext])
    return ("unsupported", None)
