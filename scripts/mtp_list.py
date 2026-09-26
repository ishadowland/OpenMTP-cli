#!/usr/bin/env python3
"""mtp-list — list connected MTP device + storage volumes.

Exits 0 if a device responds to ``device-info`` within the timeout, 1 otherwise.
The output JSON is printed to stdout regardless of exit code, so callers can
inspect ``connected`` without parsing stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _openmtp as om  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtp-list",
        description="List connected Android MTP devices and their storage volumes.",
    )
    parser.add_argument(
        "--mtp-cli-path",
        help="Override path to the mtp-cli binary (default: auto-discover via "
             "$OPENMTP_CLI or /Volumes/OpenMTP*/OpenMTP.app/...)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for mtp-cli to respond before declaring no device. "
             "Default: 5.",
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
        if args.no_json:
            print(f"Error: {e}", file=sys.stderr)
        else:
            om.emit_json({"connected": False, "mtp_cli_path": None, "error": str(e)})
        return 1

    result = om.run_mtp(
        ["version", "device-info", "storage-list"],
        mtp_cli_path=mtp_cli_path,
        timeout=args.timeout,
    )

    if result.timed_out:
        # Most common case: no device plugged in, or device not in MTP mode.
        payload = {
            "connected": False,
            "mtp_cli_path": str(mtp_cli_path),
            "error": (
                f"mtp-cli did not respond within {args.timeout}s. "
                "Likely causes: no Android device plugged in, USB mode not set "
                "to 'File transfer / MTP', or device is locked."
            ),
        }
        if args.no_json:
            print(f"Not connected: {payload['error']}", file=sys.stderr)
        else:
            om.emit_json(payload)
        return 1

    device_info = om.parse_device_info(result.stdout)
    storages = om.parse_storage_list(result.stdout)

    # ``device-info`` returning non-empty key/value pairs is the strongest signal
    # that a device is connected. Some libmtp builds return ``{}`` for the
    # metadata even when a device is present, so fall back to the storages list.
    connected = bool(device_info) or bool(storages)

    payload = {
        "connected": connected,
        "mtp_cli_path": str(mtp_cli_path),
        "device": device_info or None,
        "storages": storages,
    }

    if args.no_json:
        print(f"Connected: {connected}")
        print(f"mtp-cli:   {mtp_cli_path}")
        if device_info:
            for key, value in device_info.items():
                print(f"  {key}: {value}")
        for s in storages:
            line = f"  storage {s.get('id', '?')}: {s.get('description', '?')}"
            if s.get("free") and s.get("total"):
                line += f" ({s['free']} free / {s['total']})"
            print(line)
        return 0 if connected else 1

    om.emit_json(payload)
    return 0 if connected else 1


if __name__ == "__main__":
    sys.exit(main())