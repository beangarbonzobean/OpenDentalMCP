"""
Plain-text extraction from HTML documents (saved web pages).

Open Dental's image share contains a lot of saved Dentrix Eligibility / payer
verification HTML reports — `*.htm` files — that already carry structured text.
There's no reason to OCR these; we just decode the bytes, strip script/style/
SVG content, and pull the readable text out.

Returns text that's substantially what the user would see when opening the file
in a browser, with one line per discrete text block.

This is read-only: it only consumes bytes. No network, no shell-out, no parsing
of attribute strings. Stdlib only.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Iterable

log = logging.getLogger(__name__)


# Tags whose entire subtree should be ignored. Only include tags that have
# both a start AND end tag — void elements like <meta> and <link> would
# leave _skip_depth incremented forever because their end tag never fires.
_SKIP_TAGS: frozenset[str] = frozenset({
    "script", "style", "svg", "noscript",
})

# Tags that should produce a line-break in the extracted text (block-level).
_BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "nav",
    "tr", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table",
    "thead", "tbody", "blockquote", "pre", "hr", "label", "form",
    "button", "option", "summary", "details",
})


class _TextExtractor(HTMLParser):
    """Walk the DOM and collect text runs, separating block-level boundaries
    with newlines. Skips script/style/svg subtrees entirely."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
        t = tag.lower()
        if t == "title":
            self._in_title = True
            return
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if t in _BLOCK_TAGS:
            self._emit_break()

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        t = tag.lower()
        if t == "title":
            self._in_title = False
            title_text = " ".join(self._title_buf).strip()
            if title_text:
                self.parts.append(title_text)
                self.parts.append("\n")
            self._title_buf.clear()
            return
        if t in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if t in _BLOCK_TAGS:
            self._emit_break()

    def handle_startendtag(self, tag: str, attrs):  # type: ignore[override]
        t = tag.lower()
        if t == "br" or t in _BLOCK_TAGS:
            self._emit_break()

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._skip_depth > 0:
            return
        if not data:
            return
        # Collapse runs of whitespace within a single text node — including
        # newlines, since HTML treats `\n` between block-level content as a
        # single space (block-tag boundaries already emit their own '\n' via
        # _emit_break, so we don't lose vertical structure).
        text = re.sub(r"\s+", " ", data)
        if text and text != " ":
            self.parts.append(text)

    def _emit_break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def get_text(self) -> str:
        joined = "".join(self.parts)
        # Collapse runs of whitespace within a line, then drop empty lines.
        lines = (re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines())
        return "\n".join(line for line in lines if line)


_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)


def _decode(file_bytes: bytes) -> str:
    """Best-effort decode of an HTML byte stream.

    Order of attempts:
      1. Byte-Order Mark (BOM) — definitive when present. Many Dentrix-side
         reports are saved as UTF-16 LE with BOM by the .NET writer.
      2. The meta charset hint scanned from the first 2 KB. If the meta hint
         is UTF-16 we can't trust the regex (the hint itself is multi-byte),
         so the BOM check above must catch it first.
      3. UTF-8 / cp1252 / latin-1 fallbacks.
    """
    if not file_bytes:
        return ""
    for bom, enc in _BOMS:
        if file_bytes.startswith(bom):
            try:
                # codecs handle BOM stripping automatically for utf-8-sig and
                # utf-16/32 (where the BOM defines the endianness).
                if enc.startswith("utf-16") or enc.startswith("utf-32"):
                    return file_bytes.decode(enc.replace("-le", "").replace("-be", ""))
                return file_bytes.decode(enc)
            except UnicodeDecodeError:
                pass

    head = file_bytes[:2048]
    m = re.search(rb'charset\s*=\s*["\']?\s*([\w\-]+)', head, re.IGNORECASE)
    candidates: list[str] = []
    if m:
        try:
            candidates.append(m.group(1).decode("ascii", errors="replace"))
        except Exception:
            pass
    candidates.extend(["utf-8", "cp1252", "latin-1"])
    for enc in candidates:
        try:
            return file_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return file_bytes.decode("utf-8", errors="replace")


def extract_html_text(file_bytes: bytes) -> str:
    """Extract readable plain text from HTML bytes.

    Returns "" if the input is empty or the parser produces no text.
    Never raises on malformed input — html.parser is lenient and we fall back
    to an empty string on any unexpected exception.
    """
    if not file_bytes:
        return ""
    try:
        text = _decode(file_bytes)
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        return parser.get_text()
    except Exception as e:  # belt and suspenders
        log.warning("html_extract failed: %s", e)
        return ""


_HTML_EXTS: frozenset[str] = frozenset({".htm", ".html", ".xhtml"})


def is_html_filename(file_name: str) -> bool:
    if not file_name or "." not in file_name:
        return False
    ext = "." + file_name.rsplit(".", 1)[-1].lower()
    return ext in _HTML_EXTS
