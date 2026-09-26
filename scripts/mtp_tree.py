#!/usr/bin/env python3
"""mtp-tree — list a directory on the connected MTP device.

By default, the wrapper drives a manual DFS via per-directory non-recursive
``lsext <path>`` invocations. This is the safe mode that works on OnePlus 12
(where ``lsext-r`` hangs and corrupts the USB stack — see issue #2). Pass
``--use-recursive`` to opt back into a single ``lsext-r`` call for devices
that support native recursive listing without hanging.

Returns a JSON array of nodes with file-type metadata (MTP ObjectFormatCode +
inferred MIME + category). Folder nodes are kept when they contain at least
one matching descendant so the tree reflects what would be backed up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _openmtp as om  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtp-tree",
        description="List a directory on the connected MTP device (manual DFS by default).",
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
        "--use-recursive",
        action="store_true",
        help="Use mtp-cli's native `lsext-r` (single recursive call) instead "
             "of the wrapper's manual DFS via per-directory `lsext <path>`. "
             "Faster on devices that support recursive listing natively, "
             "but **hangs on OnePlus 12** and similar OPPO-derived devices. "
             "Default: false (manual DFS, the safe option).",
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

    nodes, errors = om.walk_tree(
        args.path,
        mtp_cli_path=mtp_cli_path,
        timeout=args.timeout,
        retries_on_hang=max(0, args.retry_on_hang),
        max_depth=args.depth,
        format_filter=args.format_filter,
        recursive=args.use_recursive,
    )

    if not nodes and not errors:
        om.die(
            f"no entries returned for path {args.path!r}. Check that the path "
            f"exists and that the device is unlocked. If you passed "
            f"--use-recursive on a OnePlus, drop the flag — the recursive "
            f"listing hangs on that device.",
            code=1,
        )

    if args.no_json:
        if errors:
            print("# Errors during walk:", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
        for n in nodes:
            if n.is_dir:
                print(f"{n.path}/  ({n.object_id:#x})")
            else:
                size = om._fmt_bytes(n.size)
                print(f"{n.path}  [{n.category}] {size}  ({n.mime}, {n.format_code:#x})")
        return 0 if not errors else 1

    payload = [n.to_dict() for n in nodes]
    if errors:
        # Embed the errors as a sidecar field; JSON consumers that expect
        # a pure array can ignore it. Use a single-key envelope for safety.
        om.emit_json({"nodes": payload, "warnings": errors})
        return 0 if payload else 1
    om.emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())