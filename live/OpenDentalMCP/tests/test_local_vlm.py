"""Tests for the local-VLM backend in preprocessing.ocr_helper.

Mocks the Ollama HTTP call and the PDF renderer via dependency injection
(`http_post`, `pdf_renderer`) so no network or pypdf calls happen.
"""

from __future__ import annotations

from typing import Any

import pytest

from preprocessing.ocr_helper import (
    LOCAL_BASE_URL_DEFAULT,
    LOCAL_FALLBACK_DEFAULT,
    LOCAL_PRIMARY_DEFAULT,
    OcrError,
    OcrResult,
    _ocr_via_local,
    _prompt_for_model,
    ocr_bytes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakePoster:
    """Stand-in for the http_post callable.

    Pre-program responses with `add_response(model, response_text)` or
    `add_failure(model, exc)`. The fake replays in FIFO per-model order.
    """

    def __init__(self) -> None:
        self.queue: dict[str, list] = {}
        self.calls: list[dict] = []

    def add_response(self, model: str, response_text: str, *, prompt_eval: int = 100, eval_count: int = 50) -> None:
        self.queue.setdefault(model, []).append(
            {"response": response_text, "prompt_eval_count": prompt_eval, "eval_count": eval_count}
        )

    def add_failure(self, model: str, message: str) -> None:
        self.queue.setdefault(model, []).append(OcrError(message))

    def __call__(self, url: str, body: dict, timeout: int) -> dict:
        model = body["model"]
        self.calls.append({"url": url, "model": model, "prompt": body.get("prompt"), "n_images": len(body.get("images", []))})
        if model not in self.queue or not self.queue[model]:
            raise AssertionError(f"FakePoster: no scripted response for model={model!r}")
        item = self.queue[model].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _renderer_for_pages(*, count: int, marker: bytes = b"PAGE"):
    """Build a fake pdf_renderer that returns `count` synthetic page bytes."""
    def _render(file_bytes: bytes, *, dpi: int) -> list[bytes]:
        return [marker + str(i).encode() for i in range(count)]
    return _render


# ---------------------------------------------------------------------------
# Prompt routing
# ---------------------------------------------------------------------------

def test_prompt_for_glm_ocr_uses_short_recognition_prompt() -> None:
    assert _prompt_for_model("glm-ocr:q8_0") == "Text Recognition:"
    assert _prompt_for_model("glm-ocr") == "Text Recognition:"


def test_prompt_for_qwen_uses_generic_local_prompt() -> None:
    p = _prompt_for_model("qwen3.5:9b")
    assert "Transcribe" in p
    assert "UNREADABLE" in p
    assert p != "Text Recognition:"


# ---------------------------------------------------------------------------
# Single-image happy path
# ---------------------------------------------------------------------------

def test_single_image_primary_succeeds() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "transcribed text")
    res = _ocr_via_local(b"\x89PNGfake", media_type="image/png", http_post=poster)
    assert isinstance(res, OcrResult)
    assert res.text == "transcribed text"
    assert res.cost_usd == 0.0
    assert res.is_unreadable is False
    assert res.input_tokens == 100
    assert res.output_tokens == 50
    assert res.model == LOCAL_PRIMARY_DEFAULT
    assert len(poster.calls) == 1
    assert poster.calls[0]["model"] == LOCAL_PRIMARY_DEFAULT
    # GLM-OCR is the default primary, so the recognition prompt should be used.
    assert poster.calls[0]["prompt"] == "Text Recognition:"


def test_unreadable_response_marks_is_unreadable() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "UNREADABLE")
    res = _ocr_via_local(b"x", media_type="image/jpeg", http_post=poster)
    assert res.is_unreadable is True
    assert res.text == ""


def test_unsupported_media_type_raises() -> None:
    poster = FakePoster()
    with pytest.raises(OcrError):
        _ocr_via_local(b"x", media_type="audio/mpeg", http_post=poster)


# ---------------------------------------------------------------------------
# Retry + fallback ladder
# ---------------------------------------------------------------------------

def test_primary_retries_once_then_succeeds() -> None:
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "transient 500")
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "ok on retry")
    res = _ocr_via_local(b"x", media_type="image/png", http_post=poster)
    assert res.text == "ok on retry"
    assert res.model == LOCAL_PRIMARY_DEFAULT
    assert len(poster.calls) == 2


def test_primary_fails_twice_fallback_succeeds() -> None:
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_response(LOCAL_FALLBACK_DEFAULT, "fallback won")
    res = _ocr_via_local(b"x", media_type="image/png", http_post=poster)
    assert res.text == "fallback won"
    assert res.model == LOCAL_FALLBACK_DEFAULT
    # 2 primary attempts + 1 fallback
    assert len(poster.calls) == 3
    assert poster.calls[-1]["model"] == LOCAL_FALLBACK_DEFAULT


def test_both_primary_and_fallback_fail_raises() -> None:
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fallback also fails")
    with pytest.raises(OcrError):
        _ocr_via_local(b"x", media_type="image/png", http_post=poster)


# ---------------------------------------------------------------------------
# Multi-page PDF
# ---------------------------------------------------------------------------

def test_pdf_renders_pages_concatenates_results() -> None:
    poster = FakePoster()
    for i in range(3):
        poster.add_response(LOCAL_PRIMARY_DEFAULT, f"page {i} text", prompt_eval=200, eval_count=80)
    renderer = _renderer_for_pages(count=3)
    res = _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                          http_post=poster, pdf_renderer=renderer)
    # Each page concatenated with two newlines.
    assert res.text == "page 0 text\n\npage 1 text\n\npage 2 text"
    # Token totals aggregated across pages
    assert res.input_tokens == 600
    assert res.output_tokens == 240
    assert res.cost_usd == 0.0
    assert len(poster.calls) == 3


def test_pdf_with_zero_pages_raises() -> None:
    poster = FakePoster()
    renderer = _renderer_for_pages(count=0)
    with pytest.raises(OcrError):
        _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                        http_post=poster, pdf_renderer=renderer)


def test_pdf_renderer_failure_raises() -> None:
    poster = FakePoster()
    def boom(file_bytes: bytes, *, dpi: int):
        raise RuntimeError("rendering exploded")
    with pytest.raises(OcrError) as ei:
        _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                        http_post=poster, pdf_renderer=boom)
    assert "pdf_render_failed" in str(ei.value)


def test_pdf_all_pages_unreadable_marks_is_unreadable() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "UNREADABLE")
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "UNREADABLE")
    renderer = _renderer_for_pages(count=2)
    res = _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                          http_post=poster, pdf_renderer=renderer)
    assert res.is_unreadable is True
    assert res.text == ""


def test_pdf_one_page_unreadable_one_ok_keeps_text() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "UNREADABLE")
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "second page is fine")
    renderer = _renderer_for_pages(count=2)
    res = _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                          http_post=poster, pdf_renderer=renderer)
    # is_unreadable only when every page is unreadable
    assert res.is_unreadable is False
    assert "second page is fine" in res.text


# ---------------------------------------------------------------------------
# Mixed-model recording
# ---------------------------------------------------------------------------

def test_mixed_models_recorded_when_fallback_engaged_per_page() -> None:
    poster = FakePoster()
    # Page 0: primary works.
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "page 0")
    # Page 1: primary fails twice, fallback succeeds.
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail again")
    poster.add_response(LOCAL_FALLBACK_DEFAULT, "page 1")
    renderer = _renderer_for_pages(count=2)
    res = _ocr_via_local(b"%PDF-fake", media_type="application/pdf",
                          http_post=poster, pdf_renderer=renderer)
    assert "page 0" in res.text
    assert "page 1" in res.text
    # Both models recorded, sorted, joined with '+'
    assert LOCAL_PRIMARY_DEFAULT in res.model
    assert LOCAL_FALLBACK_DEFAULT in res.model
    assert "+" in res.model


# ---------------------------------------------------------------------------
# Override env vars / kwargs
# ---------------------------------------------------------------------------

def test_explicit_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_VLM_BASE_URL", "http://wrong:9999")
    monkeypatch.setenv("LOCAL_VLM_PRIMARY", "wrong-model")
    poster = FakePoster()
    poster.add_response("custom-model", "ok")
    res = _ocr_via_local(
        b"x", media_type="image/png",
        base_url="http://right:8888",
        primary_model="custom-model",
        http_post=poster,
    )
    assert res.text == "ok"
    assert poster.calls[0]["url"].startswith("http://right:8888/")


def test_prompt_override_used_for_primary() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "ok")
    _ocr_via_local(
        b"x", media_type="image/png",
        prompt="CUSTOM PROMPT",
        http_post=poster,
    )
    assert poster.calls[0]["prompt"] == "CUSTOM PROMPT"


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def test_ocr_bytes_local_backend_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_BACKEND", "local")
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "via env")

    # Patch the helper so it picks up our fake poster.
    import preprocessing.ocr_helper as oh
    real_local = oh._ocr_via_local
    captured: dict[str, Any] = {}

    def patched_local(file_bytes, **kwargs):
        captured.update(kwargs)
        kwargs["http_post"] = poster
        return real_local(file_bytes, **kwargs)

    monkeypatch.setattr(oh, "_ocr_via_local", patched_local)
    res = oh.ocr_bytes(b"x", media_type="image/png")
    assert res.text == "via env"
    assert res.cost_usd == 0.0


def test_ocr_bytes_haiku_backend_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The default route is haiku, which raises OcrConfigError without a key.
    from preprocessing.ocr_helper import OcrConfigError
    with pytest.raises(OcrConfigError):
        ocr_bytes(b"x", media_type="image/png")


def test_ocr_bytes_auto_falls_back_to_haiku_on_local_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_BACKEND", "auto")
    import preprocessing.ocr_helper as oh

    def local_explode(file_bytes, **kwargs):
        raise OcrError("local broken")

    haiku_called = {"n": 0}
    def fake_haiku(file_bytes, **kwargs):
        haiku_called["n"] += 1
        return OcrResult(text="haiku ran", model="claude-haiku-4-5",
                         input_tokens=10, output_tokens=5, cost_usd=0.0001,
                         media_type=kwargs.get("media_type", "image/png"),
                         is_unreadable=False)

    monkeypatch.setattr(oh, "_ocr_via_local", local_explode)
    monkeypatch.setattr(oh, "_ocr_via_haiku", fake_haiku)
    res = oh.ocr_bytes(b"x", media_type="image/png")
    assert res.text == "haiku ran"
    assert haiku_called["n"] == 1


def test_ocr_bytes_local_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """When OCR_BACKEND=local explicitly, errors propagate (no auto-haiku)."""
    monkeypatch.setenv("OCR_BACKEND", "local")
    import preprocessing.ocr_helper as oh

    def local_explode(file_bytes, **kwargs):
        raise OcrError("local broken")

    monkeypatch.setattr(oh, "_ocr_via_local", local_explode)
    with pytest.raises(OcrError):
        oh.ocr_bytes(b"x", media_type="image/png")
