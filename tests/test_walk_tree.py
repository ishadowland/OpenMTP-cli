"""Tests for the wrapper-side manual DFS in ``_openmtp.walk_tree``.

These exercise the workaround that avoids ``lsext-r`` (which hangs on
OnePlus 12) by calling non-recursive ``lsext <path>`` per directory.
The injected runner returns canned ``MTPResult`` values per ``lsext``
invocation so no real device is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _openmtp as om  # noqa: E402


def _mkrunner(per_command_results):
    """Return a runner that yields the next MTPResult for the command it
    receives. ``per_command_results`` is a list of (predicate, MTPResult)
    pairs; the first predicate that matches the lsext command wins.
    """
    queue = list(per_command_results)

    def runner(commands, *, mtp_cli_path, timeout, extra_args=(), retries_on_hang=0):
        joined = " ".join(commands)
        for predicate, result in queue:
            if predicate(joined):
                return result
        raise AssertionError(f"no scripted result for command: {joined!r}")

    return runner


def _lsext_mtp_result(body: str) -> om.MTPResult:
    """Wrap a fake mtp-cli body in a healthy MTPResult."""
    return om.MTPResult(
        stdout=body,
        stderr="IOCreatePlugInInterfaceForService: error 0xe00002be\n",
        timed_out=False,
        exit_code=0,
        attempts=1,
        last_command="lsext",
    )


class TestWalkTreeNonRecursive:
    def test_returns_root_children_only(self):
        # Only files at root — no subdirectories to recurse into, so
        # walk_tree's single 'lsext' call is sufficient.
        body = """
59         65537      3801     524288 2024-01-28 15:24:58  IMG_001.jpg
116        65537      b901   1048576 2026-06-06 10:18:23  VID_001.mp4
58         65537      3004       2048 2026-09-26 10:58:28  notes.txt
""".strip()
        runner = _mkrunner([(lambda c: c == "lsext", _lsext_mtp_result(body))])
        nodes, errors = om.walk_tree("/", mtp_cli_path=Path("/fake"), timeout=5, run_fn=runner)
        assert errors == []
        names = sorted(n.name for n in nodes)
        assert names == ["IMG_001.jpg", "VID_001.mp4", "notes.txt"]
        paths = sorted(n.path for n in nodes)
        assert paths == ["IMG_001.jpg", "VID_001.mp4", "notes.txt"]

    def test_descends_into_subdirectories(self):
        root_body = """
116        65537      3001          0 2026-06-06 10:18:23  DCIM
""".strip()
        dcim_body = """
120        65537      3801     578688 2026-06-06 10:18:23  IMG_001.jpg
121        65537      3801     423456 2026-06-06 10:18:24  IMG_002.jpg
""".strip()
        runner = _mkrunner([
            (lambda c: c == "lsext", _lsext_mtp_result(root_body)),
            (lambda c: c == "lsext DCIM", _lsext_mtp_result(dcim_body)),
        ])
        nodes, errors = om.walk_tree(
            "/", mtp_cli_path=Path("/fake"), timeout=5, run_fn=runner,
        )
        assert errors == []
        assert len(nodes) == 3  # DCIM + 2 JPEGs
        # DCIM at depth 1 (one segment, no slash); JPEGs at depth 2
        # (DCIM/IMG.jpg has one slash → depth 2).
        def depth(path):
            return 0 if path == "" else path.count("/") + 1
        depths = sorted(depth(n.path) for n in nodes)
        assert depths == [1, 2, 2]

    def test_records_timeout_per_subdirectory(self):
        """A subdirectory that hangs should not abort the entire walk;
        the error is recorded and the rest of the tree continues."""
        root_body = """
116        65537      3001          0 2026-06-06 10:18:23  DCIM
117        65537      3001          0 2026-09-26 10:58:28  Camera
""".strip()
        hung = om.MTPResult(
            stdout="",
            stderr="",
            timed_out=True,
            exit_code=-1,
            attempts=3,
            last_command="lsext DCIM",
        )
        camera_body = """
130        65537      3801     424556 2026-05-02 08:41:06  IMG_001.jpg
""".strip()
        runner = _mkrunner([
            (lambda c: c == "lsext", _lsext_mtp_result(root_body)),
            (lambda c: c == "lsext DCIM", hung),
            (lambda c: c == "lsext Camera", _lsext_mtp_result(camera_body)),
        ])
        nodes, errors = om.walk_tree(
            "/", mtp_cli_path=Path("/fake"), timeout=5, run_fn=runner,
        )
        # Camera subdirectory was successfully walked; DCIM subdirectory
        # recorded an error.
        assert any("DCIM" in e for e in errors)
        names = {n.name for n in nodes}
        assert "IMG_001.jpg" in names  # from Camera

    def test_max_depth_caps_recursion(self):
        root_body = """
116        65537      3001          0 2026-06-06 10:18:23  DCIM
""".strip()
        dcim_body = """
120        65537      3801     578688 2026-06-06 10:18:23  IMG_001.jpg
""".strip()
        runner = _mkrunner([
            (lambda c: c == "lsext", _lsext_mtp_result(root_body)),
            (lambda c: c == "lsext DCIM", _lsext_mtp_result(dcim_body)),
        ])
        nodes, errors = om.walk_tree(
            "/", mtp_cli_path=Path("/fake"), timeout=5, run_fn=runner,
            max_depth=1,
        )
        # max_depth=1 means root's children only — DCIM listed, its
        # children (img_001.jpg) NOT descended into.
        names = {n.name for n in nodes}
        assert names == {"DCIM"}


class TestWalkTreeRecursiveFlag:
    def test_recursive_true_calls_lsext_r(self, monkeypatch):
        body = """
120        65537      3801     578688 2026-06-06 10:18:23  IMG_001.jpg
""".strip()
        captured = []

        def fake_run_mtp(commands, **kwargs):
            captured.append(" ".join(commands))
            return _lsext_mtp_result(body)

        monkeypatch.setattr(om, "run_mtp", fake_run_mtp)
        nodes, errors = om.walk_tree(
            "/DCIM", mtp_cli_path=Path("/fake"), timeout=5,
            recursive=True,
        )
        # Should have called lsext-r /DCIM exactly once.
        assert captured == ["lsext-r /DCIM"]
        assert errors == []
        assert any(n.name == "IMG_001.jpg" for n in nodes)

    def test_recursive_true_with_timeout_returns_partial(self, monkeypatch):
        """If lsext-r hangs, walk_tree returns whatever partial stdout
        was buffered + an error message — better than nothing."""

        partial_body = """
120        65537      3801     578688 2026-06-06 10:18:23  IMG_001.jpg
""".strip()
        result = om.MTPResult(
            stdout=partial_body,
            stderr="",
            timed_out=True,
            exit_code=-1,
            attempts=2,
            last_command="lsext-r /DCIM",
        )

        monkeypatch.setattr(om, "run_mtp", lambda commands, **kw: result)

        nodes, errors = om.walk_tree(
            "/DCIM", mtp_cli_path=Path("/fake"), timeout=5,
            recursive=True,
        )
        assert any("timed out" in e for e in errors)
        # The partial body should still parse.
        assert any(n.name == "IMG_001.jpg" for n in nodes)


class TestWalkTreePathConstruction:
    def test_subpath_prefix_is_correct(self):
        """Paths in the returned tree are slash-joined from the device root."""
        root_body = """
116        65537      3001          0 2026-06-06 10:18:23  DCIM
""".strip()
        dcim_body = """
120        65537      3001          0 2026-06-06 10:18:23  Camera
""".strip()
        camera_body = """
131        65537      3801     578688 2026-06-06 10:18:23  IMG_001.jpg
""".strip()
        runner = _mkrunner([
            (lambda c: c == "lsext", _lsext_mtp_result(root_body)),
            (lambda c: c == "lsext DCIM", _lsext_mtp_result(dcim_body)),
            (lambda c: c == "lsext DCIM/Camera", _lsext_mtp_result(camera_body)),
        ])
        nodes, _ = om.walk_tree(
            "/", mtp_cli_path=Path("/fake"), timeout=5, run_fn=runner,
        )
        paths = {n.path for n in nodes}
        assert paths == {"DCIM", "DCIM/Camera", "DCIM/Camera/IMG_001.jpg"}