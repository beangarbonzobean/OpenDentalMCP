"""GPU + Ollama state probe.

Always queries Ollama HTTP API (cheap, no auth). Optionally enriches with
nvidia-smi over SSH if SSH was opened on the GPU host
(see reference_ssh_gpu_host.md). Falls back gracefully when SSH is unavailable.

Synchronous — meant to be called from the Flask request handler with a
short cache. Cheap enough that polling every 10s is fine.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("LOCAL_VLM_BASE_URL", "http://192.168.127.78:11434")
GPU_SSH_HOST = os.environ.get("GPU_SSH_HOST", "")  # e.g. "Administrator@192.168.127.78"
GPU_SSH_TIMEOUT = float(os.environ.get("GPU_SSH_TIMEOUT", "3"))


def probe() -> dict:
    """Single GPU/Ollama probe. Returns a dict suitable for storage.write_gpu."""
    out: dict = {"source": "ollama-only"}

    loaded = _ollama_ps()
    if loaded is not None:
        out["loaded_models"] = loaded
        # VRAM used = sum(size_vram across loaded models)
        out["vram_used_mb"] = sum(m.get("size_vram_mb", 0) for m in loaded)

    nvsmi = _nvidia_smi()
    if nvsmi:
        out.update(nvsmi)
        out["source"] = "ollama+nvidia-smi"

    return out


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _ollama_ps() -> Optional[list[dict]]:
    """Hit /api/ps. Returns list of loaded models or None on error."""
    url = f"{OLLAMA_BASE_URL}/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError) as e:
        log.warning("ollama /api/ps failed: %s", e)
        return None

    models = []
    for m in data.get("models", []):
        size_vram = int(m.get("size_vram", 0))
        models.append({
            "name": m.get("name"),
            "size_vram_mb": size_vram // (1024 * 1024) if size_vram else 0,
            "expires_at": m.get("expires_at"),
        })
    return models


# ---------------------------------------------------------------------------
# nvidia-smi over SSH (best-effort)
# ---------------------------------------------------------------------------

_NVIDIA_SMI_QUERY = (
    "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,power.draw "
    "--format=csv,noheader,nounits"
)


def _nvidia_smi() -> Optional[dict]:
    """Run nvidia-smi over SSH. Returns dict or None if SSH isn't configured/working."""
    if not GPU_SSH_HOST:
        return None
    if not shutil.which("ssh"):
        return None

    cmd = [
        "ssh",
        "-o", "ConnectTimeout=2",
        "-o", "BatchMode=yes",       # never prompt for password — fail fast
        "-o", "StrictHostKeyChecking=accept-new",
        GPU_SSH_HOST,
        f"nvidia-smi {_NVIDIA_SMI_QUERY}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GPU_SSH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("nvidia-smi ssh failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("nvidia-smi ssh exit %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return None

    # First GPU only. Format: "12, 5, 8765, 24576, 215.34"
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "sm_pct": int(parts[0]),
            "mem_pct": int(parts[1]),
            "vram_used_mb": int(parts[2]),
            "vram_total_mb": int(parts[3]),
            "power_w": int(float(parts[4])),
        }
    except (ValueError, IndexError) as e:
        log.warning("nvidia-smi parse failed: %s line=%r", e, line)
        return None
