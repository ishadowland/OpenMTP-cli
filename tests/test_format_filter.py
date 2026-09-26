"""Tests for MTP ObjectFormatCode categorization and MIME lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _openmtp as om  # noqa: E402


class TestCategoryForCode:
    @pytest.mark.parametrize(
        "code, expected",
        [
            (om.MTP_FORMAT_ASSOCIATION, "folder"),
            (0x3801, "image"),  # JPEG
            (0x3807, "image"),  # PNG
            (0x380B, "image"),  # GIF
            (0xB901, "video"),  # MP4
            (0xB983, "video"),  # QuickTime
            (0xB219, "audio"),  # MP3
            (0xB21C, "audio"),  # WAV
            (om.MTP_FORMAT_TEXT, "document"),  # 0x3004
            (om.MTP_FORMAT_HTML, "document"),  # 0x3005
            (om.MTP_FORMAT_XML, "document"),  # 0x300A
            (om.MTP_FORMAT_MSWORD, "document"),  # 0x300B
            (om.MTP_FORMAT_MSEXCEL, "document"),  # 0x300C
            (om.MTP_FORMAT_MSPPT, "document"),  # 0x300D
            (0x3003, "executable"),
            (om.MTP_FORMAT_UNDEFINED, "other"),  # 0x3000 — text on some devices
            (0xBABE, "other"),
        ],
    )
    def test_categories(self, code, expected):
        assert om.category_for_code(code) == expected


class TestMimeForCode:
    def test_known_mimes(self):
        assert om.mime_for_code(0x3801) == "image/jpeg"
        assert om.mime_for_code(0x3807) == "image/png"
        assert om.mime_for_code(0xB219) == "audio/mpeg"
        assert om.mime_for_code(0xB901) == "video/mp4"
        assert om.mime_for_code(om.MTP_FORMAT_ASSOCIATION) == "inode/directory"
        assert om.mime_for_code(om.MTP_FORMAT_MSWORD) == "application/msword"

    def test_unknown_falls_back_to_octet_stream(self):
        assert om.mime_for_code(0xBEEF) == "application/octet-stream"


class TestNormalizeFilter:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("image", "image"),
            ("Image", "image"),
            ("images", "image"),
            ("photo", "image"),
            ("photos", "image"),
            ("video", "video"),
            ("videos", "video"),
            ("movie", "video"),
            ("audio", "audio"),
            ("music", "audio"),
            ("document", "document"),
            ("documents", "document"),
            ("all", "all"),
            ("any", "all"),
            ("*", "all"),
            ("image/jpeg", "image"),
            ("video/mp4", "video"),
            ("audio/mpeg", "audio"),
            ("application/pdf", "document"),
            ("text/plain", "document"),
        ],
    )
    def test_known_aliases(self, value, expected):
        assert om.normalize_filter(value) == expected

    @pytest.mark.parametrize("bad", ["", "wat", "spreadsheet", "binary"])
    def test_unknown_raises(self, bad):
        with pytest.raises(ValueError):
            om.normalize_filter(bad)