"""Tests for mtp-cli output parsing.

These exercise ``parse_lsext``, ``attach_paths``, ``parse_storage_list``, and
``parse_device_info`` against synthetic but plausible mtp-cli output. Real
output from ``android-file-transfer-linux`` may vary across builds — keep these
tests close to the parser if the wire format shifts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _openmtp as om  # noqa: E402


# A realistic lsext-r output, hand-rolled to match the regex in _openmtp.py.
SAMPLE_LSEXT = """\
object id: 0x00000001, parent: 0xFFFFFFFF, name: DCIM, type: folder, size: 0, mtime: 2024-01-01T00:00:00Z, mtp object format = 0x3001
object id: 0x00000002, parent: 0x00000001, name: Camera, type: folder, size: 0, mtime: 2024-01-01T00:00:00Z, mtp object format = 0x3001
object id: 0x00000003, parent: 0x00000002, name: IMG_001.jpg, type: file, size: 5242880, mtime: 2024-09-01T10:30:00Z, mtp object format = 0x3801
object id: 0x00000004, parent: 0x00000002, name: IMG_002.jpg, type: file, size: 6291456, mtime: 2024-09-02T11:15:00Z, mtp object format = 0x3801
object id: 0x00000005, parent: 0x00000002, name: VID_001.mp4, type: file, size: 104857600, mtime: 2024-09-03T19:45:00Z, mtp object format = 0xB901
object id: 0x00000006, parent: 0x00000001, name: Backup, type: folder, size: 0, mtime: 2024-01-01T00:00:00Z, mtp object format = 0x3001
object id: 0x00000007, parent: 0x00000006, name: notes.txt, type: file, size: 2048, mtime: 2024-08-15T09:00:00Z, mtp object format = 0x3004
"""


class TestParseLsext:
    def test_counts(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        assert len(nodes) == 7

    def test_recognizes_image(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        img = [n for n in nodes if n.object_id == 0x00000003][0]
        assert img.is_dir is False
        assert img.size == 5242880
        assert img.format_code == 0x3801
        assert img.mime == "image/jpeg"
        assert img.category == "image"

    def test_recognizes_video(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        vid = [n for n in nodes if n.object_id == 0x00000005][0]
        assert vid.category == "video"
        assert vid.mime == "video/mp4"

    def test_recognizes_text_document(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        txt = [n for n in nodes if n.object_id == 0x00000007][0]
        assert txt.category == "document"
        assert txt.mime == "text/plain"

    def test_folder_recognized(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        folders = [n for n in nodes if n.is_dir]
        assert len(folders) == 3
        assert all(n.category == "folder" for n in folders)

    def test_blank_lines_ignored(self):
        messy = "\n\n" + SAMPLE_LSEXT + "\n\n"
        assert len(om.parse_lsext(messy)) == 7


class TestAttachPaths:
    def test_root_takes_its_name(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        roots = [n for n in attached if n.parent_id == 0xFFFFFFFF]
        assert len(roots) == 1
        # The root of a tree takes its own name as its path; descendants are
        # rooted underneath that name, so the structure is unambiguous without
        # a "." sentinel.
        assert roots[0].path == "DCIM"

    def test_preorder_traversal(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        paths = [n.path for n in attached]
        # Parents must come before their children.
        for n in attached:
            if n.parent_id == 0xFFFFFFFF:
                # Root — there's no parent to check.
                continue
            parent_path = next(
                (other.path for other in attached if other.object_id == n.parent_id),
                None,
            )
            assert parent_path is not None
            assert paths.index(parent_path) < paths.index(n.path)

    def test_nested_paths(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        by_id = {n.object_id: n for n in attached}
        assert by_id[0x00000003].path == "DCIM/Camera/IMG_001.jpg"
        assert by_id[0x00000005].path == "DCIM/Camera/VID_001.mp4"
        assert by_id[0x00000007].path == "DCIM/Backup/notes.txt"


class TestFilterNodes:
    def test_format_filter_keeps_folder_with_matching_child(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        images = om.filter_nodes(attached, format_filter="image")
        paths = {n.path for n in images}
        # Folders that hold at least one image are kept.
        assert "DCIM/Camera/IMG_001.jpg" in paths
        assert "DCIM/Camera/IMG_002.jpg" in paths
        # The empty-text folder is dropped (no images under it).
        assert "DCIM/Backup" not in paths
        # The video is dropped.
        assert "DCIM/Camera/VID_001.mp4" not in paths

    def test_include_glob(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        only_001 = om.filter_nodes(attached, include=["*001.*"])
        paths = {n.path for n in only_001 if not n.is_dir}
        assert paths == {"DCIM/Camera/IMG_001.jpg", "DCIM/Camera/VID_001.mp4"}

    def test_exclude_drops_files(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        no_videos = om.filter_nodes(attached, exclude=["*.mp4"])
        paths = {n.path for n in no_videos if not n.is_dir}
        assert "DCIM/Camera/VID_001.mp4" not in paths
        assert "DCIM/Camera/IMG_001.jpg" in paths

    def test_format_filter_all_keeps_everything(self):
        nodes = om.parse_lsext(SAMPLE_LSEXT)
        attached = om.attach_paths(nodes)
        assert len(om.filter_nodes(attached, format_filter="all")) == len(attached)


class TestParseStorageList:
    def test_two_storages(self):
        raw = """\
Storage: 0x00010001  Phone storage  123.45 GB free / 256.00 GB total
Storage: 0x00020001  SD card       50.00 GB free / 128.00 GB total
"""
        out = om.parse_storage_list(raw)
        assert len(out) == 2
        assert out[0]["id"] == "0x00010001"
        assert out[0]["description"] == "Phone storage"
        assert out[0]["free"] == "123.45 GB"
        assert out[0]["total"] == "256.00 GB"

    def test_garbage_lines_ignored(self):
        raw = "blah blah\nStorage: 0x1 foo 10 B free / 20 B total\nmore noise\n"
        out = om.parse_storage_list(raw)
        assert len(out) == 1
        assert out[0]["id"] == "0x1"

    def test_storage_without_capacity(self):
        raw = "Storage: 0x00010001  Phone storage\n"
        out = om.parse_storage_list(raw)
        assert out[0]["description"] == "Phone storage"
        assert "free" not in out[0]

    def test_decimal_id_positional_format(self):
        # Verbatim from issue #1: the v3.9-2 build bundled with OpenMTP.app
        # emits decimal storage ids with a ``volume:, description:`` payload
        # instead of the ``Storage: 0x...`` prefix.
        raw = "65537    volume: , description: 内部共享存储空间\n"
        out = om.parse_storage_list(raw)
        assert len(out) == 1
        assert out[0]["id"] == "65537"
        assert out[0]["description"] == "内部共享存储空间"
        assert "free" not in out[0]

    def test_decimal_and_hex_formats_together(self):
        raw = (
            "Storage: 0x00010001  Phone storage  100 GB free / 256 GB total\n"
            "65537    volume: , description: 内部共享存储空间\n"
        )
        out = om.parse_storage_list(raw)
        assert len(out) == 2
        assert out[0]["id"] == "0x00010001"
        assert out[1]["id"] == "65537"

    def test_iokit_noise_lines_skipped(self):
        # Lines that look like libmtp / IOKit status output should be skipped,
        # not misinterpreted as storage records.
        raw = (
            "IOCreatePlugInInterfaceForService(...): error 0xe00002be\n"
            "selected storage 65537  内部共享存储空间\n"
            "65537    volume: , description: 内部共享存储空间\n"
        )
        out = om.parse_storage_list(raw)
        assert len(out) == 1
        assert out[0]["id"] == "65537"


class TestParseDeviceInfo:
    def test_basic_fields(self):
        raw = """\
Manufacturer: Google
Model: Pixel 8
Device version: 14
Serial number: ABC123
"""
        info = om.parse_device_info(raw)
        assert info["manufacturer"] == "Google"
        assert info["model"] == "Pixel 8"
        assert info["device version"] == "14"
        assert info["serial number"] == "ABC123"

    def test_empty_input(self):
        assert om.parse_device_info("") == {}

    def test_bare_value_positional_format(self):
        # Verbatim from issue #1: the v3.9-2 build bundled with OpenMTP.app
        # emits fixed-order bare values instead of ``Manufacturer: ...``.
        raw = """\
OnePlus
PJD110
1.0
E0F91C3176A64C078D2C01B9C94F975D
microsoft.com: 1.0; android.com: 1.0;
"""
        info = om.parse_device_info(raw)
        assert info["manufacturer"] == "OnePlus"
        assert info["model"] == "PJD110"
        assert info["device version"] == "1.0"
        assert info["serial number"] == "E0F91C3176A64C078D2C01B9C94F975D"
        assert "extended property 1" in info
        assert info["extended property 1"] == "microsoft.com: 1.0; android.com: 1.0;"

    def test_iokit_and_storage_noise_skipped(self):
        # Real mtp-cli output mixes libmtp status noise with device data. The
        # parser must skip IOKit / "selected storage" / "Storage:" / volume
        # lines and still extract the device fields from the bare values.
        raw = """\
IOCreatePlugInInterfaceForService(...): error 0xe00002be
IOCreatePlugInInterfaceForService(...): error 0xe00002be
selected storage 65537  内部共享存储空间
OnePlus
PJD110
1.0
E0F91C3176A64C078D2C01B9C94F975D
microsoft.com: 1.0; android.com: 1.0;
65537    volume: , description: 内部共享存储空间
"""
        info = om.parse_device_info(raw)
        assert info["manufacturer"] == "OnePlus"
        assert info["model"] == "PJD110"
        assert info["device version"] == "1.0"
        assert info["serial number"] == "E0F91C3176A64C078D2C01B9C94F975D"
        # No "storage" key leaked in from the "Storage:" or volume lines.
        assert "storage" not in info

    def test_explicit_key_wins_over_positional(self):
        # If a build emits both ``Manufacturer: X`` AND a bare value line at
        # the same position, the explicit key takes precedence.
        raw = """\
Manufacturer: Google
Pixel 8
1.0
ABC123
"""
        info = om.parse_device_info(raw)
        assert info["manufacturer"] == "Google"
        # The bare "Pixel 8" lands in the model slot, which the explicit key
        # for manufacturer did NOT pre-fill, so positional still works there.
        assert info["model"] == "Pixel 8"