"""
One-shot maintenance script: delete cache rows that are stuck in terminal
states which the new pipeline now handles correctly.

Run this AFTER deploying the html_extract + tmp_artifact + path-resolver fixes,
ONCE. It deletes:

  * Status='unsupported' rows whose FileName is .htm/.html/.xhtml — the new
    pipeline extracts text from these directly instead of skipping them.
  * Status='unreadable' rows that match the tmp-artifact filename pattern —
    these are now terminal-unsupported and won't be wastefully retried.

`error` rows are NOT touched — those are already retried automatically by the
backfill, so the new path-resolver fix recovers them on the next nightly run.

Read-only against OD's database. Writes only to the local cache (deletes rows).

Usage:
    python scripts/cleanup_stale_unsupported.py --dry-run   # see what would be deleted
    python scripts/cleanup_stale_unsupported.py             # actually delete
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))

from preprocessing import document_text_cache as cache  # noqa: E402

_TMP_ARTIFACT_RE = re.compile(
    r"^(?:\d+_)?tmp[0-9a-fA-F]+(?:\.tmp)?\.(?:png|jpg|jpeg)$", re.IGNORECASE
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print counts, don't actually delete.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cache_path = cache.init_cache()
    conn = sqlite3.connect(str(cache_path))
    conn.row_factory = sqlite3.Row

    # 1. Stale unsupported HTML rows.
    html_rows = conn.execute(
        "SELECT DocNum, FileName FROM doc_text "
        "WHERE Status='unsupported' "
        "AND ("
        "  LOWER(FileName) LIKE '%.htm' "
        "  OR LOWER(FileName) LIKE '%.html' "
        "  OR LOWER(FileName) LIKE '%.xhtml'"
        ")"
    ).fetchall()
    print(f"unsupported HTML rows: {len(html_rows)}")

    # 2. tmp.png artifacts marked unreadable.
    candidate_rows = conn.execute(
        "SELECT DocNum, FileName FROM doc_text "
        "WHERE Status='unreadable' AND LOWER(FileName) LIKE 'tmp%' "
    ).fetchall()
    tmp_rows = [r for r in candidate_rows if _TMP_ARTIFACT_RE.match(r["FileName"] or "")]
    print(f"unreadable tmp-artifact rows: {len(tmp_rows)}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return 0

    targets = [r["DocNum"] for r in html_rows] + [r["DocNum"] for r in tmp_rows]
    if not targets:
        print("Nothing to delete.")
        return 0

    # Delete in batches to avoid SQLite parameter-count limits.
    BATCH = 500
    deleted = 0
    for i in range(0, len(targets), BATCH):
        chunk = targets[i:i + BATCH]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM doc_text WHERE DocNum IN ({placeholders})", chunk,
        )
        deleted += cur.rowcount or 0
    conn.commit()
    print(f"Deleted {deleted} cache rows. Next nightly backfill will re-process them.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
