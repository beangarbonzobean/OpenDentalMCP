"""Tests for preprocessing.ocr_helper.classify_extension."""

from __future__ import annotations

import pytest

from preprocessing.ocr_helper import classify_extension


@pytest.mark.parametrize("name,expected_kind,expected_mt", [
    ("photo.jpg", "image", "image/jpeg"),
    ("photo.JPG", "image", "image/jpeg"),
    ("scan.jpeg", "image", "image/jpeg"),
    ("card.png", "image", "image/png"),
    ("anim.gif", "image", "image/gif"),
    ("art.webp", "image", "image/webp"),
    ("scan.tif", "image", "image/tiff"),
    ("scan.tiff", "image", "image/tiff"),
    ("scan.bmp", "image", "image/bmp"),
    ("eob.pdf", "pdf", "application/pdf"),
    ("eob.PDF", "pdf", "application/pdf"),
])
def test_supported_extensions(name: str, expected_kind: str, expected_mt: str) -> None:
    kind, mt = classify_extension(name)
    assert kind == expected_kind
    assert mt == expected_mt


@pytest.mark.parametrize("name", [
    "xray.dcm",
    "xray.DCM",
    "xray.dxr",
    "video.mp4",
    "archive.zip",
    "binary.exe",
    "noext",
    "",
    "trailing.",
    "doc.docx",        # we don't OCR Word docs
    "spreadsheet.xlsx",
])
def test_unsupported_extensions(name: str) -> None:
    kind, mt = classify_extension(name)
    assert kind == "unsupported"
    assert mt is None
