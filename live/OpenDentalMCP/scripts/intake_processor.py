"""
CLI entry point for the intake auto-filing processor.

Run from Task Scheduler every 5-15 minutes, or manually for one-off runs:

    .venv\\Scripts\\python.exe scripts\\intake_processor.py \
        --watch=\\\\SERVER12\\OpenDentImages\\_Intake\\Pending \
        --auto-file-threshold=0.95

Reads OD API + DB credentials from MCP_CONFIG_FILE (config.prod.json) the
same way the rest of the live service does.

Logs to live/OpenDentalMCP/logs/intake_processor.log.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Intake auto-filing processor.")
    p.add_argument(
        "--watch", required=False,
        default=os.environ.get("INTAKE_WATCH_FOLDER", ""),
        help="Watch folder containing batch-scan PDFs to process. May also be "
             "set via INTAKE_WATCH_FOLDER env var.",
    )
    p.add_argument(
        "--auto-file-threshold", type=float,
        default=float(os.environ.get("INTAKE_AUTO_FILE_THRESHOLD", "0.95")),
        help="Min overall_confidence to auto-file (default 0.95). Set to "
             "1.5 to disable auto-file (everything queues for review).",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def _configure_logging(level: str) -> None:
    log_dir = _PKG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "intake_processor.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
    ))
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(file_handler)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(stdout)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)
    log = logging.getLogger("intake_processor")

    if not args.watch:
        log.error("--watch (or INTAKE_WATCH_FOLDER) is required")
        return 2

    watch = Path(args.watch)
    log.info("intake processor: watch=%s threshold=%.2f", watch, args.auto_file_threshold)

    import mcp_tools  # type: ignore[import-not-found]
    from preprocessing.intake import processor

    tools = mcp_tools.OpenDentalMCPTools()  # type: ignore[attr-defined]

    res = processor.process_watch_folder(
        watch_folder=watch,
        auto_file_threshold=args.auto_file_threshold,
        search_patients_fn=tools._search_patients,
        od_uploader_fn=tools._upload_document,
    )
    log.info("intake processor result: %s", json.dumps(asdict(res), default=str))
    return 0 if res.halted_reason is None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
