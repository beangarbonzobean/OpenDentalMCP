"""SQLite-backed snapshot store for the utilization dashboard.

Three tables, append-only timeseries. Read-latest queries take the row
with the largest `ts` per table; history queries scan a range.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(os.environ.get(
    "UTILIZATION_DB_PATH",
    Path(__file__).resolve().parent / "data" / "utilization.db",
))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_snapshot (
    ts                       TEXT PRIMARY KEY,           -- ISO UTC of scrape
    plan                     TEXT,                       -- e.g. "Max (20x)"
    session_pct              REAL,                       -- 0-100
    session_resets_in_text   TEXT,                       -- "1 hr 49 min"
    session_resets_at_iso    TEXT,                       -- computed reset time
    weekly_all_pct           REAL,
    weekly_sonnet_pct        REAL,
    weekly_design_pct        REAL,
    weekly_resets_at_text    TEXT,                       -- "Tue 3:00 PM"
    daily_routines_used      INTEGER,
    daily_routines_cap       INTEGER,
    api_extra_spent_usd      REAL,
    api_extra_cap_usd        REAL,
    api_extra_pct            REAL,
    api_extra_balance_usd    REAL,
    api_extra_resets_text    TEXT,                       -- "Jun 1"
    raw_text                 TEXT                        -- full scrape for debug/reparse
);
CREATE INDEX IF NOT EXISTS idx_claude_ts ON claude_snapshot(ts DESC);

CREATE TABLE IF NOT EXISTS gpu_snapshot (
    ts                  TEXT PRIMARY KEY,
    sm_pct              INTEGER,
    mem_pct             INTEGER,
    vram_used_mb        INTEGER,
    vram_total_mb       INTEGER,
    power_w             INTEGER,
    loaded_models_json  TEXT,                            -- JSON array
    source              TEXT                             -- 'ollama-only' | 'ollama+nvidia-smi'
);
CREATE INDEX IF NOT EXISTS idx_gpu_ts ON gpu_snapshot(ts DESC);

CREATE TABLE IF NOT EXISTS pipeline_snapshot (
    ts                       TEXT PRIMARY KEY,
    total_rows               INTEGER,
    rows_last_hour           INTEGER,
    rows_last_24h            INTEGER,
    haiku_spend_24h_usd      REAL,
    rebuild_running          INTEGER,                    -- 0/1
    models_24h_json          TEXT                        -- {"qwen2.5vl:7b": 374, ...}
);
CREATE INDEX IF NOT EXISTS idx_pipeline_ts ON pipeline_snapshot(ts DESC);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as db:
        db.executescript(_SCHEMA)


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# claude_snapshot
# ---------------------------------------------------------------------------

CLAUDE_COLS = [
    "ts", "plan", "session_pct", "session_resets_in_text", "session_resets_at_iso",
    "weekly_all_pct", "weekly_sonnet_pct", "weekly_design_pct", "weekly_resets_at_text",
    "daily_routines_used", "daily_routines_cap",
    "api_extra_spent_usd", "api_extra_cap_usd", "api_extra_pct",
    "api_extra_balance_usd", "api_extra_resets_text", "raw_text",
]


def write_claude(snap: dict) -> str:
    """Write a Claude snapshot. Sets `ts` to now if not provided. Returns ts."""
    init_db()
    snap = {**snap}
    snap.setdefault("ts", _now_iso())
    cols = [c for c in CLAUDE_COLS if c in snap]
    placeholders = ",".join(["?"] * len(cols))
    with conn() as db:
        db.execute(
            f"INSERT OR REPLACE INTO claude_snapshot ({','.join(cols)}) VALUES ({placeholders})",
            [snap[c] for c in cols],
        )
    return snap["ts"]


def read_claude_latest() -> Optional[dict]:
    init_db()
    with conn() as db:
        row = db.execute(
            "SELECT * FROM claude_snapshot ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# gpu_snapshot
# ---------------------------------------------------------------------------

GPU_COLS = [
    "ts", "sm_pct", "mem_pct", "vram_used_mb", "vram_total_mb",
    "power_w", "loaded_models_json", "source",
]


def write_gpu(snap: dict) -> str:
    init_db()
    snap = {**snap}
    snap.setdefault("ts", _now_iso())
    if "loaded_models" in snap and "loaded_models_json" not in snap:
        snap["loaded_models_json"] = json.dumps(snap.pop("loaded_models"))
    cols = [c for c in GPU_COLS if c in snap]
    placeholders = ",".join(["?"] * len(cols))
    with conn() as db:
        db.execute(
            f"INSERT OR REPLACE INTO gpu_snapshot ({','.join(cols)}) VALUES ({placeholders})",
            [snap[c] for c in cols],
        )
    return snap["ts"]


def read_gpu_latest() -> Optional[dict]:
    init_db()
    with conn() as db:
        row = db.execute(
            "SELECT * FROM gpu_snapshot ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("loaded_models_json"):
            try:
                d["loaded_models"] = json.loads(d["loaded_models_json"])
            except json.JSONDecodeError:
                d["loaded_models"] = []
        return d


# ---------------------------------------------------------------------------
# pipeline_snapshot
# ---------------------------------------------------------------------------

PIPELINE_COLS = [
    "ts", "total_rows", "rows_last_hour", "rows_last_24h",
    "haiku_spend_24h_usd", "rebuild_running", "models_24h_json",
]


def write_pipeline(snap: dict) -> str:
    init_db()
    snap = {**snap}
    snap.setdefault("ts", _now_iso())
    if "models_24h" in snap and "models_24h_json" not in snap:
        snap["models_24h_json"] = json.dumps(snap.pop("models_24h"))
    cols = [c for c in PIPELINE_COLS if c in snap]
    placeholders = ",".join(["?"] * len(cols))
    with conn() as db:
        db.execute(
            f"INSERT OR REPLACE INTO pipeline_snapshot ({','.join(cols)}) VALUES ({placeholders})",
            [snap[c] for c in cols],
        )
    return snap["ts"]


def read_pipeline_latest() -> Optional[dict]:
    init_db()
    with conn() as db:
        row = db.execute(
            "SELECT * FROM pipeline_snapshot ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("models_24h_json"):
            try:
                d["models_24h"] = json.loads(d["models_24h_json"])
            except json.JSONDecodeError:
                d["models_24h"] = {}
        return d
