"""
Selectively re-OCR rows the cache has marked Status='unreadable'.

`unreadable` is a terminal status: the nightly backfill skips those rows so
they don't get re-attempted on every run. That is the right default — most
unreadable scans (genuinely blank pages, faxes that came through black) will
keep coming back unreadable.

But two things make a row worth retrying:

1. **The model improved.** When you swap the primary VLM (glm-ocr -> qwen2.5vl)
   or change the prompt, past unreadables might transcribe successfully under
   the new setup.
2. **A bug got fixed.** The UTF-16 BOM bug we just patched cached HTML files as
   3-char garbage, which the new MIN_OK_CHARS floor would now demote to
   unreadable. Those need re-processing.

Common patterns:

    # Re-attempt every row whose OcrModel != "qwen2.5vl:7b" (i.e. anything
    # OCR'd before tonight's prompt change):
    python scripts/retry_unreadable.py --not-from-model qwen2.5vl:7b --dry-run

    # Re-attempt every unreadable row regardless of model:
    python scripts/retry_unreadable.py --all

    # Re-attempt unreadable html_extract rows older than a given timestamp
    # (e.g. before the UTF-16 fix landed):
    python scripts/retry_unreadable.py --model html_extract --before 2026-05-05

    # Same, but limit to a category (e.g. only insurance reports):
    python scripts/retry_unreadable.py --model html_extract --doc-category 461

The script DELETES the matching cache rows so the next nightly backfill picks
them up naturally — there is no per-row OCR call here. `--dry-run` prints the
counts without deleting.

Read-only against OD's database. Writes only to the local cache (deletes rows).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))

from preprocessing import document_text_cache as cache  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = p.add_argument_group("selection")
    sel.add_argument("--all", action="store_true",
                     help="Re-OCR every Status='unreadable' row.")
    sel.add_argument("--model",
                     help="Only rows whose OcrModel exactly matches this string.")
    sel.add_argument("--not-from-model",
                     help="Only rows whose OcrModel does NOT match this string.")
    sel.add_argument("--before",
                     help="Only rows OCR'd before this ISO timestamp "
                          "(e.g. 2026-05-05 or 2026-05-05T12:00:00).")
    sel.add_argument("--doc-category", type=int,
                     help="Only rows with this DocCategory DefNum.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print matching count, don't delete.")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N rows (0 = no limit). Useful for testing "
                        "with --dry-run before a full retry.")
    return p


def _build_query(args: argparse.Namespace) -> tuple[str, list]:
    where = ["Status = 'unreadable'"]
    params: list = []
    if args.model:
        where.append("OcrModel = ?")
        params.append(args.model)
    if args.not_from_model:
        # COALESCE so NULL OcrModel rows still match a "not equal" filter.
        where.append("COALESCE(OcrModel, '') != ?")
        params.append(args.not_from_model)
    if args.before:
        where.append("OcrAt < ?")
        params.append(args.before)
    if args.doc_category is not None:
        where.append("DocCategory = ?")
        params.append(int(args.doc_category))
    sql = "SELECT DocNum, FileName, OcrModel, OcrAt FROM doc_text WHERE " + " AND ".join(where)
    sql += " ORDER BY DocNum"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return sql, params


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not (args.all or args.model or args.not_from_model
            or args.before or args.doc_category is not None):
        print("error: specify at least one selection flag (--all / --model / "
              "--not-from-model / --before / --doc-category). "
              "Run with --help for examples.", file=sys.stderr)
        return 2

    cache_path = cache.init_cache()
    conn = sqlite3.connect(str(cache_path))
    conn.row_factory = sqlite3.Row

    sql, params = _build_query(args)
    rows = conn.execute(sql, params).fetchall()
    print(f"Matching rows: {len(rows)}")
    if not rows:
        return 0

    by_model: dict[str, int] = {}
    for r in rows:
        m = r["OcrModel"] or "(none)"
        by_model[m] = by_model.get(m, 0) + 1
    print("By model:")
    for m, n in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {m}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return 0

    targets = [r["DocNum"] for r in rows]
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
    print(f"\nDeleted {deleted} cache rows. Next nightly backfill will re-process them.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
