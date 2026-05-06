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
    health_check_local_vlm,
    ocr_bytes,
    reset_circuit_breaker,
)


@pytest.fixture(autouse=True)
def _reset_circuit():
    """Each test starts with a clean circuit-breaker state — failures from a
    previous test must not leak into this one."""
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


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
        self.calls.append({
            "url": url, "model": model,
            "prompt": body.get("prompt"),
            "n_images": len(body.get("images", [])),
            "keep_alive": body.get("keep_alive"),
        })
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


# ---------------------------------------------------------------------------
# Per-page Haiku fallback (3rd tier)
# ---------------------------------------------------------------------------

def _fake_haiku(text: str, *, cost: float = 0.01, in_tok: int = 200, out_tok: int = 100):
    """Build a fake haiku_caller that returns a fixed OcrResult."""
    def _call(page_bytes: bytes, media_type: str) -> OcrResult:
        return OcrResult(
            text=text, model="claude-haiku-4-5-20251001",
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=cost, media_type=media_type, is_unreadable=False,
        )
    return _call


def test_haiku_page_fallback_disabled_by_default() -> None:
    """When haiku_page_fallback=False, both local tiers failing raises OcrError."""
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fail 3")
    haiku_called = {"n": 0}
    def haiku_caller(*a, **kw):
        haiku_called["n"] += 1
        return _fake_haiku("haiku saved")(*a, **kw)
    with pytest.raises(OcrError):
        _ocr_via_local(b"x", media_type="image/png", http_post=poster,
                        haiku_caller=haiku_caller, haiku_page_fallback=False)
    assert haiku_called["n"] == 0


def test_request_includes_default_keep_alive() -> None:
    """Each /api/generate request carries keep_alive so the Ollama daemon
    knows when to unload the model (LABCOMPUTER is a shared GPU; we don't
    want qwen2.5vl holding 17 GB indefinitely)."""
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "ok")
    _ocr_via_local(b"x", media_type="image/png", http_post=poster)
    assert poster.calls[0]["keep_alive"] == "30s"


def test_request_keep_alive_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_VLM_KEEP_ALIVE", "5m")
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "ok")
    _ocr_via_local(b"x", media_type="image/png", http_post=poster)
    assert poster.calls[0]["keep_alive"] == "5m"


def test_haiku_page_fallback_uses_original_media_type_for_image() -> None:
    """JPEG inputs that fall through to Haiku must declare image/jpeg, not
    image/png. The earlier hardcoding made Haiku reject genuine JPEG bytes
    with a 400 invalid_request_error."""
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p0 fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p0 fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "p0 fallback fail")
    captured: list[str] = []
    def haiku_caller(page_bytes, media_type):
        captured.append(media_type)
        return _fake_haiku("haiku ok")(page_bytes, media_type)
    res = _ocr_via_local(
        b"\xff\xd8\xff\xe0fake-jpeg",  # SOI marker
        media_type="image/jpeg",
        http_post=poster,
        haiku_page_fallback=True,
        haiku_caller=haiku_caller,
    )
    assert res.text == "haiku ok"
    # Critical assertion: Haiku saw image/jpeg, not image/png.
    assert captured == ["image/jpeg"]


def test_haiku_page_fallback_uses_png_for_pdf_rendered_pages() -> None:
    """PDF inputs are rendered to PNG by PyMuPDF — the per-page media-type
    should be image/png, regardless of the original application/pdf input."""
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fallback fail")
    captured: list[str] = []
    def haiku_caller(page_bytes, media_type):
        captured.append(media_type)
        return _fake_haiku("haiku ok")(page_bytes, media_type)
    renderer = _renderer_for_pages(count=1)
    res = _ocr_via_local(
        b"%PDF-fake",
        media_type="application/pdf",
        http_post=poster, pdf_renderer=renderer,
        haiku_page_fallback=True,
        haiku_caller=haiku_caller,
    )
    assert res.text == "haiku ok"
    assert captured == ["image/png"]


def test_haiku_page_fallback_enabled_rescues_failed_page() -> None:
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fail 3")
    res = _ocr_via_local(
        b"x", media_type="image/png",
        http_post=poster,
        haiku_page_fallback=True,
        haiku_caller=_fake_haiku("haiku saved", cost=0.012),
    )
    assert res.text == "haiku saved"
    assert "claude-haiku" in res.model
    assert res.cost_usd == pytest.approx(0.012)
    assert res.input_tokens == 200
    assert res.output_tokens == 100


def test_haiku_page_fallback_also_failing_raises() -> None:
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fail 3")
    def haiku_explodes(page_bytes, media_type):
        raise OcrError("haiku also broken")
    with pytest.raises(OcrError) as ei:
        _ocr_via_local(b"x", media_type="image/png",
                        http_post=poster,
                        haiku_page_fallback=True,
                        haiku_caller=haiku_explodes)
    assert "+haiku" in str(ei.value)


def test_haiku_page_fallback_env_var_enables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_VLM_HAIKU_PAGE_FALLBACK", "true")
    poster = FakePoster()
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "fail 3")
    res = _ocr_via_local(
        b"x", media_type="image/png",
        http_post=poster,
        haiku_caller=_fake_haiku("env enabled"),
    )
    assert res.text == "env enabled"


def test_haiku_page_fallback_only_for_failing_pages_in_pdf() -> None:
    """In a 3-page PDF where page 2 fails on both local models, only page 2
    incurs Haiku cost. Pages 1 and 3 stay free."""
    poster = FakePoster()
    # Page 0 local primary OK
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "page 0 local")
    # Page 1: primary fails twice + fallback fails
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p1 fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p1 fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "p1 fallback fail")
    # Page 2 local primary OK
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "page 2 local")
    renderer = _renderer_for_pages(count=3)
    res = _ocr_via_local(
        b"%PDF-fake", media_type="application/pdf",
        http_post=poster, pdf_renderer=renderer,
        haiku_page_fallback=True,
        haiku_caller=_fake_haiku("page 1 via haiku", cost=0.013),
    )
    assert "page 0 local" in res.text
    assert "page 1 via haiku" in res.text
    assert "page 2 local" in res.text
    # Only one page hit Haiku, so cost = one Haiku call.
    assert res.cost_usd == pytest.approx(0.013)
    # Both local primary and Haiku appear in models_used; fallback doesn't because
    # it failed on the only page that needed it.
    assert LOCAL_PRIMARY_DEFAULT in res.model
    assert "claude-haiku" in res.model


def test_haiku_page_fallback_not_called_when_local_succeeds() -> None:
    poster = FakePoster()
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "local handled it")
    haiku_called = {"n": 0}
    def haiku_caller(*a, **kw):
        haiku_called["n"] += 1
        return _fake_haiku("never used")(*a, **kw)
    res = _ocr_via_local(
        b"x", media_type="image/png",
        http_post=poster,
        haiku_page_fallback=True,
        haiku_caller=haiku_caller,
    )
    assert res.text == "local handled it"
    assert res.cost_usd == 0.0
    assert haiku_called["n"] == 0


# ---------------------------------------------------------------------------
# Circuit breaker (Fix #2)
# ---------------------------------------------------------------------------

def test_circuit_breaker_trips_after_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once N consecutive Ollama failures pile up across pages, subsequent
    pages skip local entirely and go straight to Haiku."""
    monkeypatch.setenv("LOCAL_VLM_CIRCUIT_TRIP_AFTER", "3")
    poster = FakePoster()
    # Page 0: fails 3x → trips breaker (3 = primary*2 + fallback*1)
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p0 fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p0 fail 2")
    poster.add_failure(LOCAL_FALLBACK_DEFAULT, "p0 fallback fail")
    # Pages 1+ should NOT call Ollama at all — no responses scripted.
    renderer = _renderer_for_pages(count=3)
    res = _ocr_via_local(
        b"%PDF-fake", media_type="application/pdf",
        http_post=poster, pdf_renderer=renderer,
        haiku_page_fallback=True,
        haiku_caller=_fake_haiku("via haiku", cost=0.005),
    )
    # All 3 pages went through Haiku
    assert res.cost_usd == pytest.approx(0.015)
    # Only 3 Ollama calls (the page-0 failures); pages 1+2 skipped local entirely
    assert len(poster.calls) == 3


def test_circuit_breaker_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful local call clears the consecutive-failure counter."""
    monkeypatch.setenv("LOCAL_VLM_CIRCUIT_TRIP_AFTER", "3")
    poster = FakePoster()
    # Page 0: 1 primary failure then primary success — 1 failure recorded, then reset
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "blip")
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "p0 ok")
    # Page 1: 2 primary failures + fallback success — 2 failures, reset
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p1 fail 1")
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p1 fail 2")
    poster.add_response(LOCAL_FALLBACK_DEFAULT, "p1 fallback ok")
    # Page 2: 1 primary failure then primary success — total consecutive never hit 3
    poster.add_failure(LOCAL_PRIMARY_DEFAULT, "p2 blip")
    poster.add_response(LOCAL_PRIMARY_DEFAULT, "p2 ok")
    renderer = _renderer_for_pages(count=3)
    res = _ocr_via_local(
        b"%PDF-fake", media_type="application/pdf",
        http_post=poster, pdf_renderer=renderer,
    )
    assert "p0 ok" in res.text
    assert "p1 fallback ok" in res.text
    assert "p2 ok" in res.text
    assert res.cost_usd == 0.0  # never hit Haiku


def test_health_check_returns_ok_when_tags_endpoint_responds() -> None:
    def fetcher(url, timeout):
        assert url.endswith("/api/tags")
        return 200, b'{"models":[]}'
    healthy, detail = health_check_local_vlm(fetcher=fetcher)
    assert healthy is True
    assert detail == "ok"


def test_health_check_trips_breaker_when_unreachable() -> None:
    def fetcher(url, timeout):
        raise ConnectionRefusedError("nope")
    healthy, detail = health_check_local_vlm(fetcher=fetcher)
    assert healthy is False
    assert "unreachable" in detail
    # Subsequent OCR call should skip local entirely
    poster = FakePoster()
    res = _ocr_via_local(
        b"x", media_type="image/png", http_post=poster,
        haiku_page_fallback=True,
        haiku_caller=_fake_haiku("haiku rescue"),
    )
    assert res.text == "haiku rescue"
    assert len(poster.calls) == 0  # zero Ollama calls — circuit pre-tripped


def test_health_check_trips_breaker_on_non_200() -> None:
    def fetcher(url, timeout):
        return 503, b"service unavailable"
    healthy, detail = health_check_local_vlm(fetcher=fetcher)
    assert healthy is False
    assert "503" in detail


def test_first_attempt_uses_fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempt 1 should use the fast-timeout ceiling, attempt 2 the full one."""
    monkeypatch.setenv("LOCAL_VLM_FIRST_ATTEMPT_TIMEOUT", "5")
    captured_timeouts: list[int] = []

    class _CapturingPoster:
        def __init__(self) -> None:
            self.attempt = 0
        def __call__(self, url, body, timeout):
            captured_timeouts.append(timeout)
            self.attempt += 1
            if self.attempt == 1:
                raise OcrError("fast attempt timed out")
            return {"response": "second attempt ok",
                    "prompt_eval_count": 100, "eval_count": 50}

    poster = _CapturingPoster()
    res = _ocr_via_local(
        b"x", media_type="image/png", http_post=poster, timeout=600,
    )
    assert res.text == "second attempt ok"
    # Fast timeout first, full timeout on retry
    assert captured_timeouts == [5, 600]
