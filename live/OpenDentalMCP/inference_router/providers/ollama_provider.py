"""Local Ollama provider — direct HTTP API calls to the GPU host."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from inference_router.providers.base import InferenceResult, Provider, ProviderError


OLLAMA_BASE_URL = os.environ.get("LOCAL_VLM_BASE_URL", "http://192.168.127.78:11434")
OLLAMA_DEFAULT_MODEL = os.environ.get("LOCAL_VLM_PRIMARY", "qwen2.5vl:7b")


class OllamaProvider(Provider):
    name = "local_ollama"

    def call(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        model_hint: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: int = 60,
        allowed_tools: Optional[list[str]] = None,  # not supported, ignored
        cwd: Optional[str] = None,                   # not supported, ignored
    ) -> InferenceResult:
        model = model_hint or OLLAMA_DEFAULT_MODEL
        body: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": max_tokens},
        }
        if images:
            body["images"] = [base64.standard_b64encode(b).decode("ascii") for b in images]

        url = f"{OLLAMA_BASE_URL}/api/generate"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(f"ollama HTTP {e.code}: {body_text}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ProviderError(f"ollama unreachable: {e}") from e
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if not isinstance(payload, dict):
            raise ProviderError(f"ollama returned non-dict: {type(payload).__name__}")

        return InferenceResult(
            text=(payload.get("response", "") or "").strip(),
            provider=self.name,
            model=model,
            input_tokens=int(payload.get("prompt_eval_count", 0) or 0),
            output_tokens=int(payload.get("eval_count", 0) or 0),
            cost_usd=0.0,
            latency_ms=elapsed_ms,
            metadata={"done_reason": payload.get("done_reason")},
        )
