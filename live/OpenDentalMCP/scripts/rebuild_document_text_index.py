"""
CLI entry point for the document_text_index backfill.

Designed to be invoked from Windows Task Scheduler nightly:

    .venv\\Scripts\\python.exe scripts\\rebuild_document_text_index.py \
        --max-docs=2000 --max-spend=2.00

Logs to live/OpenDentalMCP/logs/document_text_rebuild.log (rotated by date).

Read-only against OD's database. Writes only to the local SQLite cache and the
log file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Make the live/OpenDentalMCP/ directory importable when launched as a script
# rather than as a module.
_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill the OD document-text cache.")
    p.add_argument("--max-docs", type=int, default=500,
                   help="Maximum docs scanned this run (cached + uncached).")
    p.add_argument("--max-spend", type=float, default=5.0, dest="max_spend",
                   help="Soft cap on Anthropic spend in USD this run.")
    p.add_argument("--prune", action="store_true",
                   help="After backfill, delete cache rows whose DocNum no longer exists in OD.")
    p.add_argument("--dry-run", action="store_true",
                   help="List would-be-OCR'd docs, make no API calls, write no cache rows.")
    p.add_argument("--after-doc-num", type=int, default=0,
                   help="Resume cursor (DocNum greater-than).")
    p.add_argument("--log-level", default="INFO")
    return p


def _configure_logging(level: str) -> None:
    log_dir = _PKG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "document_text_rebuild.log"
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
    ))
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)
    # Mirror to stdout for Task Scheduler "Last result" surface.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(stream)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)
    log = logging.getLogger("rebuild_document_text_index")

    # Lazy imports so the CLI can --help without pulling the world.
    import mcp_tools  # type: ignore[import-not-found]
    from preprocessing import document_text_index as idx

    tools = mcp_tools.OpenDentalMCPTools()  # type: ignore[attr-defined]
    log.info(
        "starting backfill: max_docs=%d max_spend=$%.2f prune=%s dry_run=%s after=%d",
        args.max_docs, args.max_spend, args.prune, args.dry_run, args.after_doc_num,
    )
    res = idx.backfill(
        tools,
        max_docs=args.max_docs,
        max_spend_usd=args.max_spend,
        after_doc_num=args.after_doc_num,
        prune=args.prune,
        dry_run=args.dry_run,
    )
    log.info("backfill result: %s", json.dumps(asdict(res), default=str))
    return 0 if res.success else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
