"""
OCR backend dispatch for dental practice documents.

Two backends:

- `haiku` (default): Claude Haiku 4.5 vision via the Anthropic API. Sends
  PDFs natively as `document` blocks. ~$0.01/doc, ~12s/doc, highest quality.

- `local`: Ollama-served vision model on a LAN host. Renders PDFs to PNG
  pages first (via preprocessing.pdf_render), OCRs each page, concatenates.
  $0/doc, ~2-5s/doc per page, quality varies by model.

Selection is via the `OCR_BACKEND` env var: `haiku` (default), `local`,
or `auto` (try local, fall back to haiku on per-doc OcrError).

The local backend uses two models in a primary/fallback ladder controlled
by `LOCAL_VLM_PRIMARY` (default `glm-ocr:q8_0`) and `LOCAL_VLM_FALLBACK`
(default `qwen3.5:9b`). On a 5xx from primary, the helper retries once,
then falls back to secondary.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost model — Haiku 4.5 (claude-haiku-4-5-20251001) public pricing
# ---------------------------------------------------------------------------
# Last verified: 2025-10. Update if prices change.
# Input:  $1 / 1M tokens
# Output: $5 / 1M tokens
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


# ---------------------------------------------------------------------------
# Local backend defaults
# ---------------------------------------------------------------------------

LOCAL_BASE_URL_DEFAULT = "http://localhost:11434"
LOCAL_PRIMARY_DEFAULT = "glm-ocr:q8_0"
LOCAL_FALLBACK_DEFAULT = "qwen3.5:9b"
LOCAL_DPI_DEFAULT = 150
LOCAL_TIMEOUT_DEFAULT = 600  # seconds per page (max — only used on retry)
# First attempt uses a tighter ceiling so a hung Ollama doesn't burn 10 minutes
# per page before failing through. Healthy calls finish in 0.5–10s; even cold-
# start of a cached model is well under 30s.
LOCAL_FIRST_ATTEMPT_TIMEOUT_DEFAULT = 30
# After this many consecutive Ollama errors in one process, trip the circuit
# breaker and skip directly to Haiku for the rest of the run. Prevents a dead
# GPU host from making every page wait 30s × N before falling through.
LOCAL_CIRCUIT_TRIP_AFTER_DEFAULT = 5

# Model-specific prompts. GLM-OCR was trained with a "Text Recognition:"
# convention; general VLMs need a more verbose instruction tuned for dental
# practice documents (consents, treatment plans, statements, EOBs, insurance
# eligibility reports, lab slips, etc).
_PROMPT_BY_MODEL_PREFIX = {
    "glm-ocr": "Text Recognition:",
}
_PROMPT_GENERIC_LOCAL = (
    "Transcribe ALL text visible in this dental practice document — "
    "printed text, handwritten notes, signatures, dates, fees, ADA codes, "
    "tooth numbers, insurance/subscriber IDs, and any tabular data. "
    "Preserve form structure with line breaks; render tables row-by-row. "
    "Do not summarize, paraphrase, or add commentary. "
    "If the image is genuinely blank or illegible, respond with the single "
    "word UNREADABLE and nothing else."
)


# Optional per-DocCategory prompt override. Populated from an env-var-pointed
# JSON file at most once per process. Keys are DocCategory DefNum (int as str
# in JSON, normalized to int here); values are full prompt strings.
#
# Example /path/to/prompts.json:
#     {"461": "This is an insurance EOB...", "454": "This is a billing statement..."}
# Configure with OCR_CATEGORY_PROMPTS_FILE=/path/to/prompts.json.
_category_prompts_cache: Optional[dict[int, str]] = None


def _load_category_prompts() -> dict[int, str]:
    global _category_prompts_cache
    if _category_prompts_cache is not None:
        return _category_prompts_cache
    path = os.environ.get("OCR_CATEGORY_PROMPTS_FILE", "").strip()
    out: dict[int, str] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for k, v in raw.items():
                try:
                    out[int(k)] = str(v)
                except (TypeError, ValueError):
                    log.warning("category_prompts: bad key %r, skipping", k)
        except FileNotFoundError:
            log.warning("OCR_CATEGORY_PROMPTS_FILE not found: %s", path)
        except Exception as e:
            log.warning("OCR_CATEGORY_PROMPTS_FILE load failed: %s", e)
    _category_prompts_cache = out
    return out


def get_prompt_for_doc_category(doc_category: Optional[int]) -> Optional[str]:
    """Return the per-category prompt override, or None to use defaults.

    Wired into the orchestrator so docs of known categories (EOBs, statements,
    consents, etc.) can be primed with field vocabulary that lifts VLM accuracy
    on structured forms — typically 5-10% on EOB-style layouts.
    """
    if doc_category is None:
        return None
    return _load_category_prompts().get(int(doc_category))


def reset_category_prompts_cache() -> None:
    """Test seam — force reload from OCR_CATEGORY_PROMPTS_FILE on next call."""
    global _category_prompts_cache
    _category_prompts_cache = None


def _prompt_for_model(model: str) -> str:
    for prefix, prompt in _PROMPT_BY_MODEL_PREFIX.items():
        if model.startswith(prefix):
            return prompt
    return _PROMPT_GENERIC_LOCAL


@dataclass
class OcrResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    media_type: str
    is_unreadable: bool


# ---------------------------------------------------------------------------
# Local-VLM circuit breaker (process-scoped state)
# ---------------------------------------------------------------------------
# Guards against a hung Ollama host wasting time across hundreds of pages. After
# `_circuit_trip_threshold` consecutive failures the circuit is "tripped" — the
# rest of the run skips local OCR and goes straight to Haiku page fallback (when
# enabled). A successful local call resets the consecutive-failure counter; the
# tripped flag persists for the lifetime of the process to avoid flapping.
_local_circuit: dict = {"consecutive_failures": 0, "tripped": False}


def _circuit_trip_threshold() -> int:
    return int(os.environ.get(
        "LOCAL_VLM_CIRCUIT_TRIP_AFTER",
        str(LOCAL_CIRCUIT_TRIP_AFTER_DEFAULT),
    ))


def _circuit_record_failure() -> None:
    _local_circuit["consecutive_failures"] += 1
    if (not _local_circuit["tripped"]
            and _local_circuit["consecutive_failures"] >= _circuit_trip_threshold()):
        _local_circuit["tripped"] = True
        log.error(
            "local-VLM circuit tripped after %d consecutive failures; "
            "skipping local for remainder of process",
            _local_circuit["consecutive_failures"],
        )


def _circuit_record_success() -> None:
    _local_circuit["consecutive_failures"] = 0


def _circuit_is_tripped() -> bool:
    return bool(_local_circuit["tripped"])


def reset_circuit_breaker() -> None:
    """Test seam — reset circuit-breaker state to defaults."""
    _local_circuit["consecutive_failures"] = 0
    _local_circuit["tripped"] = False


def health_check_local_vlm(
    *,
    base_url: Optional[str] = None,
    timeout: float = 5.0,
    fetcher=None,
) -> tuple[bool, str]:
    """Probe the Ollama /api/tags endpoint. Returns (is_healthy, detail).

    If the host returns a non-200 or doesn't respond within `timeout`, trip the
    circuit breaker so the run skips local OCR entirely. Cheap (~ms when up,
    `timeout` when down) — call this once at backfill start when OCR_BACKEND
    is `local` or `auto`.

    `fetcher(url, timeout) -> (status_code, body_bytes)` is a test seam.
    """
    base = (base_url or os.environ.get("LOCAL_VLM_BASE_URL", LOCAL_BASE_URL_DEFAULT)).rstrip("/")
    url = f"{base}/api/tags"
    if fetcher is None:
        def fetcher(u, t):
            req = urllib.request.Request(u, method="GET")
            with urllib.request.urlopen(req, timeout=t) as resp:
                return resp.status, resp.read()
    try:
        status, _body = fetcher(url, timeout)
    except Exception as e:
        _local_circuit["tripped"] = True
        return False, f"unreachable:{type(e).__name__}:{e}"
    if status != 200:
        _local_circuit["tripped"] = True
        return False, f"http_{status}"
    return True, "ok"


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
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = 3,
    initial_backoff_seconds: float = 2.0,
    client_factory=None,
    backend: Optional[str] = None,
) -> OcrResult:
    """Dispatch to the configured OCR backend.

    Backend selection (in order of precedence):
      1. The `backend` keyword argument (mostly for tests).
      2. The `OCR_BACKEND` env var: `haiku`, `local`, or `auto`.
      3. Default: `haiku`.

    For backwards compatibility, the keyword arguments map onto the Haiku
    backend's signature. The local backend ignores `client_factory` and
    `max_retries` (it has its own retry logic) and reads its connection
    settings from env vars (LOCAL_VLM_BASE_URL, LOCAL_VLM_PRIMARY, etc).
    """
    chosen = (backend or os.environ.get("OCR_BACKEND", "haiku")).lower()
    if chosen == "local":
        return _ocr_via_local(file_bytes, media_type=media_type, prompt=prompt)
    if chosen == "auto":
        try:
            return _ocr_via_local(file_bytes, media_type=media_type, prompt=prompt)
        except OcrError as e:
            log.warning("local OCR failed (%s); falling back to haiku", e)
            return _ocr_via_haiku(
                file_bytes,
                media_type=media_type,
                prompt=prompt or GENERIC_OCR_PROMPT,
                model=model or DEFAULT_MODEL,
                max_tokens=max_tokens,
                max_retries=max_retries,
                initial_backoff_seconds=initial_backoff_seconds,
                client_factory=client_factory,
            )
    return _ocr_via_haiku(
        file_bytes,
        media_type=media_type,
        prompt=prompt or GENERIC_OCR_PROMPT,
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        max_retries=max_retries,
        initial_backoff_seconds=initial_backoff_seconds,
        client_factory=client_factory,
    )


def _ocr_via_haiku(
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
# Local backend (Ollama-served vision model)
# ---------------------------------------------------------------------------

def _ocr_via_local(
    file_bytes: bytes,
    *,
    media_type: str,
    prompt: Optional[str] = None,
    base_url: Optional[str] = None,
    primary_model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    dpi: Optional[int] = None,
    timeout: Optional[int] = None,
    haiku_page_fallback: Optional[bool] = None,
    http_post=None,
    pdf_renderer=None,
    haiku_caller=None,
) -> OcrResult:
    """OCR via an Ollama-served vision model on the LAN.

    For PDFs: renders each page to PNG (PyMuPDF), OCRs each page, concatenates.
    For images: sends the bytes directly.

    Per-page ladder:
      1. Primary local model (e.g. glm-ocr:q8_0), attempt 1
      2. Primary local model, retry once
      3. Fallback local model (e.g. qwen3.5:9b), single attempt
      4. (Optional) Claude Haiku via API, single attempt — gated by
         LOCAL_VLM_HAIKU_PAGE_FALLBACK=true. Adds ~$0.01 per failed page
         but rescues pages that crash both local models.

    If every tier fails, raises OcrError for that page.

    Test seams:
      - http_post(url, body) -> dict       mocks the Ollama call
      - pdf_renderer(bytes, dpi) -> [bytes] mocks the PDF renderer
      - haiku_caller(bytes, media_type) -> OcrResult  mocks the Haiku call
    """
    base = (base_url or os.environ.get("LOCAL_VLM_BASE_URL", LOCAL_BASE_URL_DEFAULT)).rstrip("/")
    primary = primary_model or os.environ.get("LOCAL_VLM_PRIMARY", LOCAL_PRIMARY_DEFAULT)
    fallback = fallback_model or os.environ.get("LOCAL_VLM_FALLBACK", LOCAL_FALLBACK_DEFAULT)
    dpi_val = dpi or int(os.environ.get("LOCAL_VLM_DPI", str(LOCAL_DPI_DEFAULT)))
    timeout_val = timeout or int(os.environ.get("LOCAL_VLM_TIMEOUT", str(LOCAL_TIMEOUT_DEFAULT)))
    if haiku_page_fallback is None:
        haiku_page_fallback = os.environ.get("LOCAL_VLM_HAIKU_PAGE_FALLBACK", "false").lower() == "true"

    # Decide on the page list (one image -> one element). Track the per-page
    # media-type because PDFs get rendered to PNG by PyMuPDF (so each page is
    # PNG bytes regardless of source), while raw image inputs keep their
    # original media-type. The Haiku page-fallback path needs the right
    # media-type to avoid `image/png` being declared for actual JPEG bytes.
    if media_type == "application/pdf":
        renderer = pdf_renderer if pdf_renderer is not None else _default_pdf_renderer
        try:
            pages = renderer(file_bytes, dpi=dpi_val)
        except Exception as e:
            raise OcrError(f"pdf_render_failed:{e}") from e
        if not pages:
            raise OcrError("pdf_no_pages")
        page_media_type = "image/png"
    elif media_type.startswith("image/"):
        pages = [file_bytes]
        page_media_type = media_type
    else:
        raise OcrError(f"Unsupported media_type: {media_type}")

    poster = http_post if http_post is not None else _default_ollama_post
    parts: list[str] = []
    total_in = 0
    total_out = 0
    page_costs: float = 0.0
    models_used: set[str] = set()
    is_unreadable_pages = 0

    for page_idx, page_bytes in enumerate(pages):
        text, model_used, in_tok, out_tok, page_cost = _ocr_page_with_fallback(
            page_bytes,
            primary=primary,
            fallback=fallback,
            base_url=base,
            timeout=timeout_val,
            prompt_override=prompt,
            poster=poster,
            page_idx=page_idx,
            haiku_page_fallback=haiku_page_fallback,
            haiku_caller=haiku_caller,
            page_media_type=page_media_type,
        )
        parts.append(text)
        total_in += in_tok
        total_out += out_tok
        page_costs += page_cost
        models_used.add(model_used)
        if text.strip().upper() == "UNREADABLE":
            is_unreadable_pages += 1

    full_text = "\n\n".join(parts).strip()
    is_unreadable = is_unreadable_pages == len(pages) and len(pages) > 0
    return OcrResult(
        text="" if is_unreadable else full_text,
        model="+".join(sorted(models_used)) if models_used else primary,
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=page_costs,  # 0 for pure-local; >0 only when Haiku page fallback engaged
        media_type=media_type,
        is_unreadable=is_unreadable,
    )


def _ocr_page_with_fallback(
    page_bytes: bytes,
    *,
    primary: str,
    fallback: str,
    base_url: str,
    timeout: int,
    prompt_override: Optional[str],
    poster,
    page_idx: int,
    haiku_page_fallback: bool = False,
    haiku_caller=None,
    page_media_type: str = "image/png",
) -> tuple[str, str, int, int, float]:
    """OCR one page with retry-once on primary, then fallback to secondary.

    Per-attempt timeout ladder:
      - primary attempt 1: min(timeout, LOCAL_VLM_FIRST_ATTEMPT_TIMEOUT)
      - primary attempt 2: full `timeout`
      - fallback model:    full `timeout`

    Circuit breaker: if the process-scoped `_local_circuit` is tripped (too
    many consecutive Ollama failures earlier in the run), local tiers are
    skipped entirely and we go straight to Haiku page fallback when enabled.

    If both local tiers fail and `haiku_page_fallback=True`, makes one final
    attempt via Claude Haiku for that page.

    Returns (text, model_used, input_tokens, output_tokens, cost_usd).
    """
    last_err: Optional[Exception] = None
    fast_timeout = min(
        int(os.environ.get(
            "LOCAL_VLM_FIRST_ATTEMPT_TIMEOUT",
            str(LOCAL_FIRST_ATTEMPT_TIMEOUT_DEFAULT),
        )),
        timeout,
    )

    if _circuit_is_tripped():
        log.debug("page %d: skipping local (circuit tripped)", page_idx)
    else:
        # Primary, attempt 1 (fast) + attempt 2 (full timeout).
        per_attempt_timeouts = [fast_timeout, timeout]
        for attempt, attempt_timeout in enumerate(per_attempt_timeouts):
            try:
                text, in_tok, out_tok = _ollama_ocr_call(
                    page_bytes,
                    model=primary,
                    base_url=base_url,
                    timeout=attempt_timeout,
                    prompt=prompt_override or _prompt_for_model(primary),
                    poster=poster,
                )
                _circuit_record_success()
                return text, primary, in_tok, out_tok, 0.0
            except OcrError as e:
                last_err = e
                _circuit_record_failure()
                log.warning("page %d %s attempt %d failed (timeout=%ds): %s",
                            page_idx, primary, attempt + 1, attempt_timeout, e)
                if _circuit_is_tripped():
                    # Trip happened mid-page — abandon further local attempts.
                    log.warning("page %d: circuit tripped mid-page, skipping fallback model", page_idx)
                    break
        else:
            # for-else: only entered if loop completed without break (i.e. circuit
            # not tripped). Fallback model, single attempt.
            if fallback and fallback != primary:
                try:
                    text, in_tok, out_tok = _ollama_ocr_call(
                        page_bytes,
                        model=fallback,
                        base_url=base_url,
                        timeout=timeout,
                        prompt=prompt_override or _prompt_for_model(fallback),
                        poster=poster,
                    )
                    _circuit_record_success()
                    log.info("page %d recovered via fallback model %s", page_idx, fallback)
                    return text, fallback, in_tok, out_tok, 0.0
                except OcrError as e:
                    last_err = e
                    _circuit_record_failure()
                    log.warning("page %d fallback %s failed: %s", page_idx, fallback, e)

    # Last-resort: Claude Haiku for this page only. Opt-in via env var.
    if haiku_page_fallback:
        try:
            caller = haiku_caller if haiku_caller is not None else _default_haiku_page_call
            # Pass the actual page media type — for PDFs PyMuPDF rendered the
            # pages to PNG, but for raw image inputs the bytes are still in
            # their original format (jpg/png/etc.). Hardcoding image/png made
            # Haiku reject genuine JPEG bytes with a 400 invalid_request_error.
            haiku_result = caller(page_bytes, page_media_type)
            log.info("page %d recovered via Haiku page fallback (cost $%.4f)",
                     page_idx, haiku_result.cost_usd)
            return (haiku_result.text, haiku_result.model,
                    haiku_result.input_tokens, haiku_result.output_tokens,
                    haiku_result.cost_usd)
        except OcrError as e:
            last_err = e
            log.warning("page %d Haiku fallback failed: %s", page_idx, e)
    raise OcrError(f"page {page_idx} failed on primary+fallback{'+haiku' if haiku_page_fallback else ''}: {last_err!r}")


def _default_haiku_page_call(page_bytes: bytes, media_type: str) -> OcrResult:
    """Real Haiku call for a single page. Tests inject their own caller."""
    return _ocr_via_haiku(
        page_bytes,
        media_type=media_type,
        prompt=GENERIC_OCR_PROMPT,
    )


def _ollama_ocr_call(
    page_bytes: bytes,
    *,
    model: str,
    base_url: str,
    timeout: int,
    prompt: str,
    poster,
) -> tuple[str, int, int]:
    """One /api/generate call. Returns (text, prompt_eval_count, eval_count)."""
    b64 = base64.standard_b64encode(page_bytes).decode("ascii")
    body = {
        "model": model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.0},
        # The Ollama host (LABCOMPUTER) is a shared workstation: the same RTX
        # 3090 Ti also serves Medit Link, Asiga Composer, Ceramill, Open
        # Dental, browser GPU acceleration, etc. qwen2.5vl:7b alone holds
        # ~17 GB of the card's 24 GB while loaded. With the default 5-minute
        # keep_alive (or worse, longer) staff hit VRAM contention during the
        # day. Override per-request so the model unloads quickly after a
        # batch burst ends, giving the rest of the box its VRAM back.
        # During an active backfill (workers=4, calls back-to-back) any value
        # >= a few seconds keeps the model warm across docs.
        "keep_alive": os.environ.get("LOCAL_VLM_KEEP_ALIVE", "30s"),
    }
    url = f"{base_url}/api/generate"
    result = poster(url, body, timeout)
    if not isinstance(result, dict):
        raise OcrError(f"ollama returned non-dict: {type(result).__name__}")
    text = result.get("response", "") or ""
    in_tok = int(result.get("prompt_eval_count", 0) or 0)
    out_tok = int(result.get("eval_count", 0) or 0)
    return text.strip(), in_tok, out_tok


def _default_ollama_post(url: str, body: dict, timeout: int) -> dict:
    """Real HTTP POST to Ollama. Tests inject their own poster."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise OcrError(f"ollama HTTP {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise OcrError(f"ollama URL error: {e}") from e
    except (TimeoutError, ConnectionError) as e:
        raise OcrError(f"ollama connection error: {e}") from e
    except Exception as e:
        raise OcrError(f"ollama unexpected error: {type(e).__name__}: {e}") from e


def _default_pdf_renderer(file_bytes: bytes, *, dpi: int) -> list[bytes]:
    """Real PDF renderer via preprocessing.pdf_render. Tests inject their own."""
    from preprocessing.pdf_render import render_pdf_pages
    return render_pdf_pages(file_bytes, dpi=dpi)


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------

# A 448x448 fully-white PNG. qwen2.5vl rejects images smaller than ~224px
# with "model runner unexpectedly stopped"; 448 is the standard ViT input
# resolution and avoids that crash path. Encoded inline (~2KB after base64)
# so warmup has no filesystem dependency.
_WARMUP_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAcAAAAHACAIAAAC6Ry8kAAAACXBIWXMAAA7EAAAOxAGVKw4b"
    + "A" * 100  # placeholder, replaced below with real bytes generated at import
)


def _build_warmup_png() -> str:
    """Generate the warmup PNG at import time so the inlined bytes can't drift
    from a working PNG. Falls back to None if pymupdf isn't available — the
    warmup will then no-op cleanly."""
    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception:
        return ""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 448, 448))
    pix.set_rect(pix.irect, (255, 255, 255))
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


_WARMUP_PNG_B64 = _build_warmup_png() or _WARMUP_PNG_B64


def warmup_local_vlm(
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 90,
    max_attempts: int = 3,
    http_post=None,
) -> tuple[bool, Optional[str]]:
    """Send a tiny dummy image to the local VLM so the cold-load failure
    (GGML_ASSERT on qwen2.5vl's first call after model load) gets absorbed
    here instead of wasting time on a real page.

    Returns (ok, error_message). ok=True if any attempt succeeded; ok=False if
    every attempt raised. Either way the caller can proceed — a False return
    just signals "the warmup didn't help, expect the real first page may still
    fail and fall through to the fallback model".
    """
    base = (base_url or os.environ.get("LOCAL_VLM_BASE_URL", LOCAL_BASE_URL_DEFAULT)).rstrip("/")
    primary = model or os.environ.get("LOCAL_VLM_PRIMARY", LOCAL_PRIMARY_DEFAULT)
    poster = http_post if http_post is not None else _default_ollama_post

    img_bytes = base64.b64decode(_WARMUP_PNG_B64)
    last_err: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            text, _, _ = _ollama_ocr_call(
                img_bytes,
                model=primary, base_url=base, timeout=timeout,
                prompt="Reply with 'ok'.",
                poster=poster,
            )
            log.info("warmup_local_vlm: %s ready (attempt %d)", primary, attempt)
            return True, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning("warmup_local_vlm: %s attempt %d failed: %s",
                        primary, attempt, last_err)
    log.warning("warmup_local_vlm: %s did not warm up after %d attempts; "
                "real pages may need fallback model", primary, max_attempts)
    return False, last_err


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
