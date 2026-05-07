"""Provider abstract base + InferenceResult dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InferenceResult:
    text: str
    provider: str                 # ProviderChoice value, e.g. "local_ollama"
    model: str = ""               # actual model used (e.g. "qwen2.5vl:7b")
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0         # 0 for local + Max-subscription routes
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Provider failed. Caller may try the fallback chain."""


class Provider(ABC):
    name: str = ""

    @abstractmethod
    def call(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        model_hint: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: int = 60,
        allowed_tools: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        write_scope: Optional[str] = None,
    ) -> InferenceResult:
        ...
