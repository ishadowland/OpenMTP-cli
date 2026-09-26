#!/usr/bin/env python3
"""mtp-tree — recursively list a directory on the connected MTP device.

Returns a JSON array of nodes with file-type metadata (MTP ObjectFormatCode +
inferred MIME + category). Folder nodes are kept when they contain at least
one matching descendant so the tree reflects what would be backed up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _openmtp as om  # noqa: E402


def depth_of(path: str) -> int:
    """Return the depth of ``path``: 1 for the tree root, 2 for its children, etc.

    For a tree rooted at ``DCIM``, depth 1 = ``DCIM``, depth 2 = ``DCIM/Camera``,
    depth 3 = ``DCIM/Camera/IMG.jpg``.
    """
    return path.count("/") + 1


def apply_filters(
    nodes: list[om.TreeNode],
    *,
    max_depth: Optional[int],
    format_filter: Optional[str],
) -> list[om.TreeNode]:
    """Apply depth + category filters. Returns nodes in pre-order."""
    # Depth filter — pre-order walk guarantees parents precede children, so
    # truncating at the first too-deep node also drops its descendants.
    if max_depth is not None:
        kept: list[om.TreeNode] = []
        for n in nodes:
            if depth_of(n.path) <= max_depth:
                kept.append(n)
            else:
                break
        nodes = kept

    if not format_filter or format_filter == "all":
        return nodes

    target = om.normalize_filter(format_filter)
    if target == "all":
        return nodes

    # Category filter: keep file nodes matching the target; keep folder nodes
    # only if they have at least one matching file descendant.
    children_by_parent: dict[int, list[om.TreeNode]] = {}
    for n in nodes:
        children_by_parent.setdefault(n.parent_id, []).append(n)

    def file_matches(n: om.TreeNode) -> bool:
        return (not n.is_dir) and n.category == target

    def folder_has_match(n: om.TreeNode) -> bool:
        for child in children_by_parent.get(n.object_id, []):
            if child.is_dir:
                if folder_has_match(child):
                    return True
            elif file_matches(child):
                return True
        return False

    return [
        n for n in nodes
        if (not n.is_dir and file_matches(n)) or (n.is_dir and folder_has_match(n))
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtp-tree",
        description="Recursively list a directory on the connected MTP device.",
    )
    parser.add_argument(
        "--path",
        default="/",
        help="Directory on the device to list (default: '/' = device root).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help="Maximum recursion depth (0 = root only, 1 = immediate children, ...). "
             "Default: unbounded.",
    )
    parser.add_argument(
        "--format-filter",
        choices=["image", "video", "audio", "document", "all"],
        default=None,
        help="Keep only files in this category (and folders that contain them).",
    )
    parser.add_argument(
        "--mtp-cli-path",
        help="Override path to the mtp-cli binary.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for mtp-cli to respond (default: 10). Recursive "
             "listings on large devices need more headroom than --list.",
    )
    parser.add_argument(
        "--retry-on-hang",
        type=int,
        default=0,
        metavar="N",
        help="If mtp-cli hangs (no response within --timeout), kill it, wait, "
             "and retry up to N times before giving up. Default: 0.",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Print human-readable text instead of JSON.",
    )
    args = parser.parse_args(argv)

    try:
        mtp_cli_path = om.find_mtp_cli(args.mtp_cli_path)
    except FileNotFoundError as e:
        om.die(str(e), code=1)

    # mtp-cli's ``lsext-r <path>`` will recurse from <path>. ``cd`` is not
    # needed; we pass the path directly. If --path is ``"/"`` we omit it to
    # list the device root.
    commands = ["lsext-r"] if args.path in ("/", "") else [f"lsext-r {args.path}"]
    result = om.run_mtp(
        commands,
        mtp_cli_path=mtp_cli_path,
        timeout=args.timeout,
        retries_on_hang=max(0, args.retry_on_hang),
    )

    if result.timed_out:
        om.die(
            f"mtp-cli did not respond within {args.timeout}s after "
            f"{result.attempts} attempt(s) "
            f"(last_command={result.last_command!r}).\n"
            + om.hang_recovery_hint(result),
            code=1,
        )

    nodes = om.parse_lsext(result.stdout)
    if not nodes:
        # The wrapper has the list but it was empty — could mean path
        # doesn't exist, OR the listing produced output the parser didn't
        # recognise. Surface the raw stdout tail so the user can see what
        # mtp-cli actually emitted.
        tail = result.stdout.strip()[-500:]
        om.die(
            f"no entries returned for path {args.path!r}. Check that the path "
            f"exists and that the device is unlocked. Raw mtp-cli output "
            f"(last 500 chars):\n{tail}",
            code=1,
        )

    nodes = om.attach_paths(nodes)
    nodes = apply_filters(
        nodes,
        max_depth=args.depth,
        format_filter=args.format_filter,
    )

    if args.no_json:
        for n in nodes:
            if n.is_dir:
                print(f"{n.path}/  ({n.object_id:#x})")
            else:
                size = om._fmt_bytes(n.size)
                print(f"{n.path}  [{n.category}] {size}  ({n.mime}, {n.format_code:#x})")
        return 0

    om.emit_json([n.to_dict() for n in nodes])
    return 0


if __name__ == "__main__":
    sys.exit(main())