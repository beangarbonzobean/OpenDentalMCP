"""Document-text-cache snapshot for the OCR pipeline panel.

Reads `data/document_text_cache.db` directly. Cheap — single SQL pass.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CACHE_DB_PATH = Path(os.environ.get(
    "DOC_TEXT_CACHE_DB",
    Path(__file__).resolve().parent.parent / "data" / "document_text_cache.db",
))

LOCK_PATH = Path(os.environ.get(
    "DOC_TEXT_REBUILD_LOCK",
    Path(__file__).resolve().parent.parent / "data" / ".rebuild.lock",
))


def probe() -> dict:
    """Return current pipeline state suitable for storage.write_pipeline()."""
    out: dict = {"rebuild_running": int(LOCK_PATH.exists())}

    if not CACHE_DB_PATH.exists():
        return out

    try:
        db = sqlite3.connect(f"file:{CACHE_DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        log.warning("cache DB open failed: %s", e)
        return out

    try:
        out["total_rows"] = db.execute("SELECT COUNT(*) FROM doc_text").fetchone()[0]

        now = datetime.now(timezone.utc)
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")

        out["rows_last_hour"] = db.execute(
            "SELECT COUNT(*) FROM doc_text WHERE OcrAt >= ?", (hour_ago,)
        ).fetchone()[0]

        out["rows_last_24h"] = db.execute(
            "SELECT COUNT(*) FROM doc_text WHERE OcrAt >= ?", (day_ago,)
        ).fetchone()[0]

        haiku_24h = db.execute(
            "SELECT COALESCE(SUM(CostUsd), 0) FROM doc_text WHERE OcrAt >= ?",
            (day_ago,),
        ).fetchone()[0]
        out["haiku_spend_24h_usd"] = round(float(haiku_24h), 4)

        # Model mix in last 24h (only OK rows, model can be a "+" combination)
        models_24h = {}
        rows = db.execute(
            "SELECT COALESCE(OcrModel, '<none>'), COUNT(*) "
            "FROM doc_text WHERE OcrAt >= ? AND Status='ok' "
            "GROUP BY OcrModel ORDER BY 2 DESC",
            (day_ago,),
        ).fetchall()
        for model, n in rows:
            models_24h[model] = n
        out["models_24h"] = models_24h

    finally:
        db.close()

    return out
