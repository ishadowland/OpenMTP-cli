#!/usr/bin/env python3
"""mtp-list — list connected MTP device + storage volumes.

Exits 0 if a device responds to ``device-info`` within the timeout, 1 otherwise.
The output JSON is printed to stdout regardless of exit code, so callers can
inspect ``connected`` without parsing stderr.

When no device is found, this script tries a small fallback chain of mtp-cli
flag combinations (default → ``-C`` → ``-e``) before declaring failure. The
last attempt's stderr (which is where mtp-cli prints IOKit errors like
``0xe00002be``) is included in the JSON output under ``mtp_cli_stderr`` so the
agent / user doesn't have to dig for it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _openmtp as om  # noqa: E402


# Truncate mtp-cli stderr at this many characters when echoing it into the
# JSON output. A noisy IOKit traceback can easily exceed 10 KB; keep the
# diagnostic short enough for the agent to ingest without overflow.
STDERR_TAIL_CHARS = 1000


def _human_print_failure(detect: om.DetectResult, mtp_cli_path: Path, timeout: float) -> None:
    last = detect.last_result
    print(f"Not connected ({len(detect.attempted)} attempt(s): {', '.join(detect.attempted)})", file=sys.stderr)
    if last.timed_out:
        print(
            f"Error: mtp-cli did not respond within {timeout}s. "
            "Likely causes: no Android device plugged in, USB mode not set "
            "to 'File transfer / MTP', or device is locked.",
            file=sys.stderr,
        )
        return
    print(
        f"Error: mtp-cli returned no device-info or storage-list for any "
        f"attempted flag combination. If mtp_cli_stderr shows IOKit errors "
        f"like 0xe00002be (kIOReturnNoDevice), USB enumeration is failing — "
        f"see references/troubleshooting.md §IOKit.",
        file=sys.stderr,
    )
    if last.stderr.strip():
        print("--- mtp-cli stderr (last {n} chars) ---".format(n=STDERR_TAIL_CHARS), file=sys.stderr)
        print(last.stderr.strip()[-STDERR_TAIL_CHARS:], file=sys.stderr)


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
        "--no-fallback",
        action="store_true",
        help="Skip the flag-combination fallback chain. Use the very first "
             "attempt only. Helpful when debugging mtp-cli flag interactions.",
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

    attempts = om.MTP_DETECT_ATTEMPTS if not args.no_fallback else om.MTP_DETECT_ATTEMPTS[:1]

    detect = om.detect_device(
        mtp_cli_path=mtp_cli_path,
        timeout=args.timeout,
        attempts=attempts,
    )

    if detect.connected:
        winning_attempt = detect.attempted[-1]
        payload = {
            "connected": True,
            "mtp_cli_path": str(mtp_cli_path),
            "device": detect.device_info or None,
            "storages": detect.storages,
            "attempted": [winning_attempt],
        }
        if args.no_json:
            print(f"Connected: True (via {winning_attempt})")
            print(f"mtp-cli:   {mtp_cli_path}")
            if detect.device_info:
                for key, value in detect.device_info.items():
                    print(f"  {key}: {value}")
            for s in detect.storages:
                line = f"  storage {s.get('id', '?')}: {s.get('description', '?')}"
                if s.get("free") and s.get("total"):
                    line += f" ({s['free']} free / {s['total']})"
                print(line)
            return 0
        om.emit_json(payload)
        return 0

    # Not connected — surface every signal we have.
    last = detect.last_result
    payload = {
        "connected": False,
        "mtp_cli_path": str(mtp_cli_path),
        "device": None,
        "storages": [],
        "attempted": detect.attempted,
    }

    if last.timed_out:
        payload["error"] = (
            f"mtp-cli did not respond within {args.timeout}s. "
            "Likely causes: no Android device plugged in, USB mode not set "
            "to 'File transfer / MTP', or device is locked."
        )
    else:
        payload["error"] = (
            f"mtp-cli returned no device-info or storage-list for any of "
            f"the {len(detect.attempted)} attempted flag combination(s): "
            f"{detect.attempted}. "
            "If mtp_cli_stderr shows IOKit errors (e.g. 0xe00002be = "
            "kIOReturnNoDevice), USB enumeration is failing — see "
            "references/troubleshooting.md §IOKit."
        )

    # Surface stderr — this is where mtp-cli writes the IOKit errors that
    # explain *why* the device wasn't detected. Without this, the agent has
    # to re-run mtp-cli manually to see them.
    stderr = last.stderr.strip()
    if stderr:
        payload["mtp_cli_stderr"] = stderr[-STDERR_TAIL_CHARS:]

    if args.no_json:
        _human_print_failure(detect, mtp_cli_path, args.timeout)
        return 1

    om.emit_json(payload)
    return 1


if __name__ == "__main__":
    sys.exit(main())