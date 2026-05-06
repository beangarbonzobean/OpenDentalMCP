"""Tests for preprocessing.html_extract."""

from __future__ import annotations

from preprocessing.html_extract import extract_html_text, is_html_filename


def test_basic_text_extraction() -> None:
    html = b"<html><body><p>Hello world</p></body></html>"
    assert extract_html_text(html) == "Hello world"


def test_strips_script_and_style() -> None:
    html = b"""
    <html>
    <head><style>p { color: red; }</style></head>
    <body>
      <script>alert('hi')</script>
      <p>Visible text</p>
    </body>
    </html>
    """
    out = extract_html_text(html)
    assert "Visible text" in out
    assert "alert" not in out
    assert "color: red" not in out


def test_strips_svg_subtree() -> None:
    html = b"""
    <body>
      <svg><path d="M0 0L10 10"/></svg>
      <p>After SVG</p>
    </body>
    """
    out = extract_html_text(html)
    assert "After SVG" in out
    assert "M0 0L10 10" not in out


def test_block_tags_create_line_breaks() -> None:
    html = b"<body><p>Line one</p><p>Line two</p><div>Line three</div></body>"
    out = extract_html_text(html)
    assert out.split("\n") == ["Line one", "Line two", "Line three"]


def test_table_rows_separated() -> None:
    html = b"""
    <table>
      <tr><td>First Name</td><td>Alham</td></tr>
      <tr><td>Last Name</td><td>Shehadeh</td></tr>
    </table>
    """
    out = extract_html_text(html)
    lines = out.split("\n")
    # Each row is on its own line; cells inside a row aren't merged but td is
    # not a block tag in our list, so they share a line.
    assert any("Alham" in l for l in lines)
    assert any("Shehadeh" in l for l in lines)
    # Verify they end up on different lines (rows are blocks).
    alham_idx = next(i for i, l in enumerate(lines) if "Alham" in l)
    shehadeh_idx = next(i for i, l in enumerate(lines) if "Shehadeh" in l)
    assert alham_idx != shehadeh_idx


def test_collapses_whitespace_inside_text() -> None:
    html = b"<p>foo    bar\n\n   baz</p>"
    assert extract_html_text(html) == "foo bar baz"


def test_handles_html_entities() -> None:
    html = b"<p>AT&amp;T &lt;tag&gt; &#34;quoted&#34;</p>"
    out = extract_html_text(html)
    assert "AT&T" in out
    assert "<tag>" in out
    assert '"quoted"' in out


def test_empty_input() -> None:
    assert extract_html_text(b"") == ""


def test_malformed_html_does_not_raise() -> None:
    # html.parser is lenient. Should produce empty or partial — never raise.
    html = b"<p>foo<p><div></table>broken</p>"
    out = extract_html_text(html)
    assert "foo" in out
    assert "broken" in out


def test_title_preserved() -> None:
    html = b"<html><head><title>My Title</title></head><body><p>Body</p></body></html>"
    out = extract_html_text(html)
    assert "My Title" in out
    assert "Body" in out


def test_charset_meta_respected() -> None:
    """A doc declaring cp1252 with non-UTF-8 bytes still decodes correctly."""
    body = "fr\xe9\xe7"  # 'fréç' in cp1252
    html = (
        b"<html><head><meta charset=\"cp1252\"></head><body><p>"
        + body.encode("cp1252")
        + b"</p></body></html>"
    )
    out = extract_html_text(html)
    assert "fréç" in out


def test_utf8_default_decode() -> None:
    body = "résumé"
    html = b"<p>" + body.encode("utf-8") + b"</p>"
    out = extract_html_text(html)
    assert "résumé" in out


def test_dentrix_eligibility_like_doc() -> None:
    """Realistic structure matching the Dentrix eligibility reports we see."""
    html = b"""<!DOCTYPE html><html><head><title>Eligibility Report</title>
    <style>body { font-family: Arial; }</style></head>
    <body>
      <div id=header-name>Alham Shehadeh</div>
      <div>Created: August 6, 2025 at 8:14 AM</div>
      <table><tr><td>Subscriber ID</td><td>233961320701</td></tr>
      <tr><td>Group #</td><td>06714-05601</td></tr></table>
      <svg><path d="M0 0"/></svg>
    </body></html>"""
    out = extract_html_text(html)
    assert "Alham Shehadeh" in out
    assert "233961320701" in out
    assert "06714-05601" in out
    assert "M0 0" not in out  # SVG content stripped
    assert "font-family" not in out  # CSS stripped


def test_utf16_le_with_bom_decoded() -> None:
    """Dentrix reports written by the .NET Scriban template engine ship as
    UTF-16 LE with BOM. The decoder must recognize the BOM."""
    html = "<html><body><p>Subscriber: Jane Doe</p></body></html>"
    raw = b"\xff\xfe" + html.encode("utf-16-le")
    out = extract_html_text(raw)
    assert "Subscriber: Jane Doe" in out


def test_utf16_be_with_bom_decoded() -> None:
    html = "<html><body><p>Hello UTF-16 BE</p></body></html>"
    raw = b"\xfe\xff" + html.encode("utf-16-be")
    out = extract_html_text(raw)
    assert "Hello UTF-16 BE" in out


def test_utf8_sig_bom_stripped() -> None:
    """UTF-8 BOM should be transparently stripped, not appear in output."""
    html = "<html><body><p>BOM-prefixed UTF-8</p></body></html>"
    raw = b"\xef\xbb\xbf" + html.encode("utf-8")
    out = extract_html_text(raw)
    assert out.startswith("BOM-prefixed UTF-8")


def test_is_html_filename() -> None:
    assert is_html_filename("Page1.htm")
    assert is_html_filename("Eligibility.HTML")
    assert is_html_filename("foo.xhtml")
    assert not is_html_filename("foo.pdf")
    assert not is_html_filename("foo.htm.pdf")  # final ext is pdf
    assert not is_html_filename("")
    assert not is_html_filename("noext")
