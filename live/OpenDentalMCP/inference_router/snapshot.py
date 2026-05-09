"""Read-only adapter to the utilization dashboard's stored quota state.

The dashboard does the scraping and stores the latest snapshot. The router
just reads. Keeping this layer thin lets us swap storage backends later
without touching decision logic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Reuse the dashboard's DB. Same env var.
import os
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "utilization_dashboard" / "data" / "utilization.db"
DB_PATH = Path(os.environ.get("UTILIZATION_DB_PATH", _DEFAULT_DB))


@dataclass
class QuotaSnapshot:
    """Latest known quota state. Any field can be None if not yet scraped."""
    ts: Optional[str] = None
    plan: Optional[str] = None
    session_pct: Optional[float] = None
    session_resets_in_text: Optional[str] = None
    weekly_all_pct: Optional[float] = None
    weekly_sonnet_pct: Optional[float] = None
    weekly_design_pct: Optional[float] = None
    api_extra_pct: Optional[float] = None
    api_extra_balance_usd: Optional[float] = None


def latest() -> QuotaSnapshot:
    """Return the most recent stored Claude snapshot. Empty fields if no scrape."""
    if not DB_PATH.exists():
        return QuotaSnapshot()
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return QuotaSnapshot()
    try:
        row = db.execute(
            "SELECT * FROM claude_snapshot ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # Table might not exist yet
        return QuotaSnapshot()
    finally:
        db.close()
    if not row:
        return QuotaSnapshot()
    d = dict(row)
    return QuotaSnapshot(
        ts=d.get("ts"),
        plan=d.get("plan"),
        session_pct=d.get("session_pct"),
        session_resets_in_text=d.get("session_resets_in_text"),
        weekly_all_pct=d.get("weekly_all_pct"),
        weekly_sonnet_pct=d.get("weekly_sonnet_pct"),
        weekly_design_pct=d.get("weekly_design_pct"),
        api_extra_pct=d.get("api_extra_pct"),
        api_extra_balance_usd=d.get("api_extra_balance_usd"),
    )
