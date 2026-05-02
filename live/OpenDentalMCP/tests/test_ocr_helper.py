"""Tests for preprocessing.ocr_helper.ocr_bytes — retry / error paths.

The Anthropic SDK is not actually called: we inject a fake client via
client_factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List

import pytest

from preprocessing.ocr_helper import (
    OcrConfigError,
    OcrError,
    OcrRateLimited,
    OcrResult,
    ocr_bytes,
)


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


@dataclass
class _FakeMessage:
    content: List[_FakeBlock]
    usage: _FakeUsage


class _FakeApiError(Exception):
    def __init__(self, status_code: int, msg: str = "boom") -> None:
        super().__init__(msg)
        self.status_code = status_code


class _FakeClient:
    """Single-purpose stand-in. The script field is a list of either:
        - _FakeMessage to return,
        - or an Exception instance to raise,
       consumed in order on each .messages.create call.
    """

    def __init__(self, script: List[Any]) -> None:
        self._script = list(script)
        self.calls: List[dict] = []

        class _Messages:
            def __init__(_self) -> None:
                pass

            def create(_self, *, model: str, max_tokens: int, messages: list) -> _FakeMessage:
                self.calls.append({"model": model, "messages": messages})
                if not self._script:
                    raise AssertionError("no scripted response left")
                item = self._script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.messages = _Messages()


def _factory(client: _FakeClient) -> Callable[[], _FakeClient]:
    return lambda: client


def _msg(text: str = "Hello world", input_tokens: int = 100, output_tokens: int = 50) -> _FakeMessage:
    return _FakeMessage(
        content=[_FakeBlock(text=text)],
        usage=_FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(OcrConfigError):
        ocr_bytes(b"x", media_type="image/jpeg")


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_msg(text="hello")])
    res = ocr_bytes(b"\x89PNG", media_type="image/png", client_factory=_factory(client))
    assert res.text == "hello"
    assert res.input_tokens == 100
    assert res.output_tokens == 50
    assert res.cost_usd > 0
    assert res.is_unreadable is False
    assert res.media_type == "image/png"
    assert len(client.calls) == 1


def test_unreadable_clears_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_msg(text="UNREADABLE")])
    res = ocr_bytes(b"x", media_type="image/jpeg", client_factory=_factory(client))
    assert res.is_unreadable is True
    assert res.text == ""


def test_pdf_media_type_uses_document_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_msg(text="pdf text")])
    ocr_bytes(b"%PDF-1.4", media_type="application/pdf", client_factory=_factory(client))
    block = client.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


def test_unsupported_media_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([])  # never used
    with pytest.raises(OcrError):
        ocr_bytes(b"x", media_type="audio/mpeg", client_factory=_factory(client))


def test_429_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_FakeApiError(429), _msg(text="ok after backoff")])
    # Set tiny backoff so test stays fast.
    res = ocr_bytes(
        b"x", media_type="image/jpeg",
        client_factory=_factory(client),
        initial_backoff_seconds=0.01,
        max_retries=3,
    )
    assert res.text == "ok after backoff"
    assert len(client.calls) == 2


def test_persistent_429_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_FakeApiError(429), _FakeApiError(429), _FakeApiError(429)])
    with pytest.raises(OcrRateLimited):
        ocr_bytes(
            b"x", media_type="image/jpeg",
            client_factory=_factory(client),
            initial_backoff_seconds=0.01,
            max_retries=3,
        )


def test_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_FakeApiError(503), _msg(text="recovered")])
    res = ocr_bytes(
        b"x", media_type="image/jpeg",
        client_factory=_factory(client),
        initial_backoff_seconds=0.01,
        max_retries=3,
    )
    assert res.text == "recovered"


def test_4xx_other_than_429_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_FakeApiError(400, "bad request")])
    with pytest.raises(OcrError):
        ocr_bytes(
            b"x", media_type="image/jpeg",
            client_factory=_factory(client),
            initial_backoff_seconds=0.01,
            max_retries=3,
        )


def test_cost_estimation_includes_input_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    client = _FakeClient([_msg(input_tokens=1000, output_tokens=200)])
    res = ocr_bytes(b"x", media_type="image/jpeg", client_factory=_factory(client))
    # input: 1000 * 1/1M = 0.001; output: 200 * 5/1M = 0.001 → total ~0.002
    assert res.cost_usd == pytest.approx(0.002)
