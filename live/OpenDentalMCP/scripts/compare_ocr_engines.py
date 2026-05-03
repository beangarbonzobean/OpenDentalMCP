"""
Compare OCR backends on a chosen document.

Runs the local VLM (glm-ocr / qwen3.5 fallback) and Claude Haiku against the
same OD document and prints both outputs side by side. Useful when triaging
quality concerns or deciding whether to switch the production backend.

Usage:
    python scripts/compare_ocr_engines.py 50983
    python scripts/compare_ocr_engines.py 50983 51005 51215
    python scripts/compare_ocr_engines.py 50983 --backends local,haiku --output cmp.md

Reads OD_DOC_ROOT and the LOCAL_VLM_* / ANTHROPIC_API_KEY env vars from the
process environment. Set MCP_CONFIG_FILE=config.prod.json before invocation.

Read-only against OD's database and the image share.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Make live/OpenDentalMCP importable when launched as a script.
_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
sys.path.insert(0, str(_PKG_DIR))


def _resolve_doc(tools, doc_num: int):
    from preprocessing.path_resolver import resolve_doc_path
    from preprocessing.sql_safety import assert_select_only

    sql = (
        "SELECT d.DocNum, d.PatNum, d.FileName, d.DateCreated, d.DocCategory, "
        "p.LName, p.FName "
        "FROM document d JOIN patient p ON d.PatNum = p.PatNum "
        f"WHERE d.DocNum = {int(doc_num)}"
    )
    assert_select_only(sql)
    res = tools._query_database(sql, limit=1)
    if isinstance(res, dict) and not res.get("success", True):
        raise RuntimeError(f"_query_database failed: {res.get('error')}")
    rows = res.get("rows", []) if isinstance(res, dict) else res
    if not rows:
        return None, None
    r = rows[0]
    path = resolve_doc_path(r["PatNum"], r.get("LName") or "", r.get("FName") or "", r["FileName"])
    return r, path


def _ocr_one(file_bytes: bytes, media_type: str, backend: str) -> tuple[str, float, str, Optional[str]]:
    """Return (text, seconds, model, error)."""
    from preprocessing import ocr_helper as oh
    t0 = time.time()
    try:
        if backend == "local":
            r = oh._ocr_via_local(file_bytes, media_type=media_type)
        elif backend == "haiku":
            r = oh._ocr_via_haiku(file_bytes, media_type=media_type, prompt=oh.GENERIC_OCR_PROMPT)
        else:
            return "", 0.0, "", f"unknown backend {backend}"
        return r.text, time.time() - t0, r.model, None
    except Exception as e:
        return "", time.time() - t0, "", f"{type(e).__name__}: {e}"


def _render_md_section(doc_num: int, file_name: str, results: dict[str, dict]) -> str:
    lines = [f"# DocNum {doc_num} — `{file_name}`", ""]
    lines.append("| Backend | Model | Chars | Seconds | Error |")
    lines.append("| :--- | :--- | ---: | ---: | :--- |")
    for backend, r in results.items():
        err = r["error"] or ""
        lines.append(
            f"| {backend} | {r['model'] or '-'} | {len(r['text'])} | "
            f"{r['seconds']:.1f} | {err[:80]} |"
        )
    lines.append("")
    for backend, r in results.items():
        lines.append(f"## {backend} output")
        lines.append("")
        lines.append("```")
        lines.append(r["text"] or f"<no text — error: {r['error']}>")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare OCR backends on OD documents.")
    p.add_argument("doc_nums", nargs="+", type=int, help="One or more DocNums.")
    p.add_argument("--backends", default="local,haiku",
                   help="Comma-separated list. Choices: local, haiku. Default: local,haiku.")
    p.add_argument("--output", default=None,
                   help="Optional Markdown file to write. If omitted, prints to stdout.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    valid = {"local", "haiku"}
    bad = [b for b in backends if b not in valid]
    if bad:
        print(f"unknown backends: {bad}; valid={sorted(valid)}", file=sys.stderr)
        return 2

    import mcp_tools  # type: ignore[import-not-found]
    from preprocessing import ocr_helper as oh

    tools = mcp_tools.OpenDentalMCPTools()  # type: ignore[attr-defined]

    sections: list[str] = []
    for doc_num in args.doc_nums:
        meta, path = _resolve_doc(tools, doc_num)
        if meta is None:
            sections.append(f"# DocNum {doc_num} — NOT FOUND in OD\n")
            continue
        if not path or not os.path.exists(str(path)):
            sections.append(f"# DocNum {doc_num} — file missing on share: `{path}`\n")
            continue
        kind, mt = oh.classify_extension(meta["FileName"])
        if kind == "unsupported" or mt is None:
            sections.append(f"# DocNum {doc_num} — unsupported file type: `{meta['FileName']}`\n")
            continue
        with open(path, "rb") as f:
            file_bytes = f.read()
        results: dict[str, dict] = {}
        for b in backends:
            text, secs, model, err = _ocr_one(file_bytes, mt, b)
            results[b] = {"text": text, "seconds": secs, "model": model, "error": err}
        sections.append(_render_md_section(doc_num, meta["FileName"], results))

    output_md = "\n---\n\n".join(sections)
    if args.output:
        Path(args.output).write_text(output_md, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(output_md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
