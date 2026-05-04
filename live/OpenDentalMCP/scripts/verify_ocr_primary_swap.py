"""Verify the OCR primary-model swap (glm-ocr -> qwen2.5-vl).

Runs a 10-doc replay with a tight Haiku spend cap, snapshots cache contents
before and after, and reports whether the new primary is actually serving
requests or whether work is still falling through to Haiku/qwen3.5 fallback.

Usage:
    .venv\\Scripts\\python.exe scripts\\verify_ocr_primary_swap.py

Pre-req:
    - Ollama running on the GPU host with `qwen2.5-vl:7b` pulled
    - LOCAL_VLM_PRIMARY env reflects the new model (set by the nightly script
      or by the user before invoking this)

Exit code 0 if the new primary is doing >=70% of new OK rows, else 1.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
DB_PATH = _PKG_DIR / "data" / "document_text_cache.db"
LOG_PATH = _PKG_DIR / "logs" / "document_text_rebuild.log"
PYTHON = _PKG_DIR / ".venv" / "Scripts" / "python.exe"
REBUILD = _THIS_DIR / "rebuild_document_text_index.py"

EXPECTED_PRIMARY = os.environ.get("LOCAL_VLM_PRIMARY", "qwen2.5vl:7b")
MAX_DOCS = 30  # scan budget — kept tight; we use --after-doc-num to skip cached zone
MAX_SPEND = 0.10  # cap Haiku rescue to $0.10 — if local fails, we want to know fast
WORKERS = 2


def _resume_cursor() -> int:
    """Start scanning past the highest cached DocNum so we hit uncached work."""
    db = sqlite3.connect(DB_PATH)
    try:
        row = db.execute("SELECT COALESCE(MAX(DocNum), 0) FROM doc_text").fetchone()
        return int(row[0])
    finally:
        db.close()


def snapshot_models() -> dict[str, int]:
    db = sqlite3.connect(DB_PATH)
    rows = db.execute(
        "SELECT COALESCE(OcrModel,'<null>'), COUNT(*) FROM doc_text "
        "WHERE Status='ok' GROUP BY OcrModel"
    ).fetchall()
    db.close()
    return {model: n for model, n in rows}


def diff_models(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys}


def tail_log_for_run(start_ts: float) -> list[str]:
    """Return log lines emitted after start_ts."""
    if not LOG_PATH.exists():
        return []
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln for ln in lines if ln >= cutoff]


def main() -> int:
    if not DB_PATH.exists():
        print(f"FATAL: cache DB not found at {DB_PATH}")
        return 2
    if not PYTHON.exists():
        print(f"FATAL: venv python not found at {PYTHON}")
        return 2

    after_cursor = _resume_cursor()

    print(f"verify_ocr_primary_swap: expected primary = {EXPECTED_PRIMARY}")
    print(f"DB: {DB_PATH}")
    print(f"Running rebuild: --max-docs={MAX_DOCS} --max-spend=${MAX_SPEND} "
          f"--workers={WORKERS} --after-doc-num={after_cursor}")
    print()

    before = snapshot_models()
    start_ts = time.time()

    cmd = [
        str(PYTHON), str(REBUILD),
        f"--max-docs={MAX_DOCS}",
        f"--max-spend={MAX_SPEND}",
        f"--workers={WORKERS}",
        f"--after-doc-num={after_cursor}",
        "--log-level=INFO",
    ]
    proc = subprocess.run(cmd, cwd=_PKG_DIR, capture_output=True, text=True)
    elapsed = time.time() - start_ts
    print(f"rebuild exit code: {proc.returncode}  elapsed: {elapsed:.1f}s")

    after = snapshot_models()
    diff = diff_models(before, after)
    new_total = sum(v for v in diff.values() if v > 0)

    print()
    print("=== Model distribution diff (status=ok rows added) ===")
    if not new_total:
        print("  (no new rows — either everything failed or all 10 were cached/skipped)")
    else:
        for model, delta in sorted(diff.items(), key=lambda kv: -kv[1]):
            if delta == 0:
                continue
            pct = 100.0 * delta / new_total if new_total else 0.0
            marker = "  *PRIMARY*" if model.startswith(EXPECTED_PRIMARY) else ""
            print(f"  {model:35s}  +{delta:3d}  ({pct:5.1f}%){marker}")

    print()
    print("=== Recent log signal ===")
    log_tail = tail_log_for_run(start_ts)
    ggml = sum(1 for ln in log_tail if "GGML_ASSERT" in ln)
    primary_fail = sum(1 for ln in log_tail
                       if EXPECTED_PRIMARY in ln and "failed" in ln)
    fallback_recovered = sum(1 for ln in log_tail if "recovered via fallback" in ln)
    haiku_recovered = sum(1 for ln in log_tail
                          if "recovered" in ln and "haiku" in ln.lower())
    print(f"  GGML_ASSERT failures (old glm bug):     {ggml}")
    print(f"  {EXPECTED_PRIMARY} failure attempts:    {primary_fail}")
    print(f"  recovered via fallback model:           {fallback_recovered}")
    print(f"  recovered via Haiku page fallback:      {haiku_recovered}")

    print()
    print("=== Verdict ===")
    primary_new = sum(v for k, v in diff.items()
                      if k.startswith(EXPECTED_PRIMARY) and v > 0)
    pct_primary = 100.0 * primary_new / new_total if new_total else 0.0
    haiku_new = sum(v for k, v in diff.items()
                    if "haiku" in k.lower() and v > 0)
    pct_haiku = 100.0 * haiku_new / new_total if new_total else 0.0

    if new_total == 0:
        print("  INCONCLUSIVE — no new docs processed. Check the log directly.")
        rc = 2
    elif pct_primary >= 70:
        print(f"  PASS — {pct_primary:.0f}% of new docs OCR'd by {EXPECTED_PRIMARY}.")
        rc = 0
    elif pct_haiku >= 50:
        print(f"  FAIL — {pct_haiku:.0f}% of new docs fell through to Haiku. "
              f"Local lane is not serving.")
        rc = 1
    else:
        print(f"  PARTIAL — {pct_primary:.0f}% on primary, {pct_haiku:.0f}% on Haiku.")
        rc = 1

    if proc.returncode != 0:
        print()
        print("=== Rebuild stderr (last 30 lines) ===")
        for ln in (proc.stderr or "").splitlines()[-30:]:
            print(f"  {ln}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
