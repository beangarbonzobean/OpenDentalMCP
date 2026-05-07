"""Routing-decision log — writes one row per route() decision to the
utilization dashboard's SQLite DB so the dashboard can show a histogram.

Uses the same DB the dashboard scrapes into; isolated table.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_DEFAULT_DB = (
    Path(__file__).resolve().parent.parent
    / "utilization_dashboard" / "data" / "utilization.db"
)
DB_PATH = Path(os.environ.get("UTILIZATION_DB_PATH", _DEFAULT_DB))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decision (
    ts             TEXT NOT NULL,
    choice         TEXT NOT NULL,        -- ProviderChoice value
    burn_mode      TEXT,                 -- BurnMode value at decision time
    reasoning      TEXT,
    profile_tag    TEXT,
    profile_json   TEXT,                 -- serialized Profile for debug
    fallbacks      TEXT,                 -- comma-sep ProviderChoice values
    outcome        TEXT,                 -- 'pending' | 'ok' | 'fallback' | 'failed'
    provider_used  TEXT,                 -- final ProviderChoice (after fallback if any)
    cost_usd       REAL,
    latency_ms     INTEGER,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_ts ON routing_decision(ts DESC);
"""


def _init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(_SCHEMA)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def log_decision(
    *,
    choice: str,
    burn_mode: str,
    reasoning: str,
    profile_tag: str,
    profile_json: str,
    fallbacks: list[str],
) -> str:
    """Record a routing decision before dispatch. Returns the ts (key)."""
    _init()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as db:
        db.execute(
            "INSERT INTO routing_decision "
            "(ts, choice, burn_mode, reasoning, profile_tag, profile_json, fallbacks, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (ts, choice, burn_mode, reasoning, profile_tag, profile_json,
             ",".join(fallbacks)),
        )
    return ts


def update_outcome(
    ts: str,
    *,
    outcome: str,
    provider_used: str = "",
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    error: str = "",
) -> None:
    _init()
    with _conn() as db:
        db.execute(
            "UPDATE routing_decision SET outcome=?, provider_used=?, "
            "cost_usd=?, latency_ms=?, error=? WHERE ts=?",
            (outcome, provider_used, cost_usd, latency_ms, error, ts),
        )


def recent(limit: int = 100) -> list[dict]:
    _init()
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM routing_decision ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def histogram_24h() -> dict:
    """Return choice-counts and provider_used-counts over the last 24h."""
    _init()
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with _conn() as db:
        decisions = dict(db.execute(
            "SELECT choice, COUNT(*) FROM routing_decision WHERE ts>=? GROUP BY choice",
            (cutoff,),
        ).fetchall())
        actuals = dict(db.execute(
            "SELECT provider_used, COUNT(*) FROM routing_decision "
            "WHERE ts>=? AND provider_used != '' GROUP BY provider_used",
            (cutoff,),
        ).fetchall())
        cost = db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM routing_decision WHERE ts>=?",
            (cutoff,),
        ).fetchone()[0] or 0.0
        burn_modes = dict(db.execute(
            "SELECT burn_mode, COUNT(*) FROM routing_decision WHERE ts>=? GROUP BY burn_mode",
            (cutoff,),
        ).fetchall())
    return {
        "decisions_24h": decisions,
        "actual_provider_24h": actuals,
        "cost_24h_usd": round(float(cost), 4),
        "burn_modes_24h": burn_modes,
    }
