#!/usr/bin/env python3
"""Shared helpers for openmtp-cli scripts.

This module is intentionally private to the ``scripts/`` directory; scripts
import it via:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _openmtp

Nothing in here is part of the public CLI contract. If you need it from
elsewhere, copy or vendor — do not add an import surface.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, NoReturn, Optional, Sequence


# -----------------------------------------------------------------------------
# Path discovery
# -----------------------------------------------------------------------------

# Default OpenMTP.app mount points on macOS. Multiple versions are observed in
# the wild (``OpenMTP 3.3.0/`` is the current one at the time of writing, but
# 3.4.0 / 4.0.0 are likely once they ship). The glob covers them.
_DEFAULT_MTP_MOUNT_GLOBS = (
    "/Volumes/OpenMTP*/OpenMTP.app/Contents/Resources/bin/mtp-cli",
)


def find_mtp_cli(override: Optional[str] = None) -> Path:
    """Locate the ``mtp-cli`` binary.

    Resolution order:
      1. ``override`` argument (typically from ``--mtp-cli-path``).
      2. ``$OPENMTP_CLI`` environment variable.
      3. Glob over ``/Volumes/OpenMTP*/OpenMTP.app/Contents/Resources/bin/mtp-cli``.

    Raises ``FileNotFoundError`` with an actionable message if nothing matches.
    """
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--mtp-cli-path does not exist: {p}")
        return p

    env = os.environ.get("OPENMTP_CLI")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"$OPENMTP_CLI does not exist: {p}")
        return p

    matches = sorted(Path("/").glob("Volumes/OpenMTP*/OpenMTP.app/Contents/Resources/bin/mtp-cli"))

    if not matches:
        raise FileNotFoundError(
            "mtp-cli not found. Tried:\n"
            "  - --mtp-cli-path argument\n"
            "  - $OPENMTP_CLI environment variable\n"
            f"  - glob {_DEFAULT_MTP_MOUNT_GLOBS!r}\n"
            "Is OpenMTP.app mounted? (Check /Volumes/.)"
        )
    if len(matches) > 1:
        # Ambiguous — print them all to stderr so the agent can disambiguate.
        print(
            f"warning: multiple OpenMTP mounts found; using {matches[0]}. "
            f"Pass --mtp-cli-path to disambiguate. Others: "
            + ", ".join(str(p) for p in matches[1:]),
            file=sys.stderr,
        )
    return matches[0]


# -----------------------------------------------------------------------------
# mtp-cli subprocess wrapper
# -----------------------------------------------------------------------------

@dataclass
class MTPResult:
    """Result of one batch-mode mtp-cli invocation.

    Attributes:
        stdout: Whatever mtp-cli printed to stdout before exit/timeout/kill.
        stderr: Whatever mtp-cli printed to stderr. IOKit errors (e.g.
            ``IOCreatePlugInInterfaceForService: error 0xe00002be``) and
            "no mtp device found" land here.
        timed_out: True if :func:`subprocess.communicate` raised
            ``TimeoutExpired`` (the wrapper killed the child and reaped it).
        exit_code: mtp-cli's exit code (or ``-1`` if killed by the wrapper).
        attempts: How many times :func:`run_mtp` actually invoked mtp-cli.
            > 1 means at least one retry was used.
        last_command: The last command (or first line of the joined
            commands sequence) the wrapper was feeding mtp-cli when this
            result was produced. Useful for diagnostics when a hang
            occurs deep in a sequence like ``device-info / select-storage
            / lsext-r``.
    """

    stdout: str
    stderr: str
    timed_out: bool
    exit_code: int
    attempts: int = 1
    last_command: str = ""


def hang_recovery_hint(result: MTPResult) -> str:
    """Return a one-line human hint to recover from a hang / IOKit stall.

    Called by the CLI scripts when ``result.timed_out`` or when stderr
    shows the canonical ``IOCreatePlugInInterfaceForService: error
    0xe00002be`` pattern. The hint points at the two known recovery
    steps (replug the cable, or ``sudo killall -HUP usbd`` to refresh
    the macOS USB stack).
    """
    cmd = f" while running {result.last_command!r}" if result.last_command else ""
    return (
        "mtp-cli did not return within the timeout"
        f"{cmd}. Recovery on macOS:\n"
        "  1. Unplug and replug the USB cable (lets the phone re-enumerate\n"
        "     its MTP endpoints).\n"
        "  2. If that does not help: 'sudo killall -HUP usbd' forces the\n"
        "     macOS USB daemon to re-enumerate. Requires sudo; briefly\n"
        "     disconnects other USB devices.\n"
        "  3. Toggle the phone's USB mode (Settings → USB preferences) from\n"
        "     'Charging only' to 'File transfer / MTP' (or vice versa) to\n"
        "     trigger a fresh USB role-switch.\n"
        "  4. As a last resort, restart the Mac.\n"
        "Pass --retry-on-hang N to have the wrapper retry the call N times\n"
        "after killing mtp-cli; useful when the hang is intermittent."
    )


def run_mtp(
    commands: Sequence[str],
    *,
    mtp_cli_path: Optional[Path] = None,
    timeout: float = 5.0,
    extra_args: Sequence[str] = (),
    retries_on_hang: int = 0,
    retry_sleep: float = 2.0,
) -> MTPResult:
    """Spawn mtp-cli in batch mode and run a sequence of commands.

    ``commands`` is joined with newlines, a trailing ``quit`` is appended, and
    the whole payload is fed to mtp-cli on stdin. Output is read until the
    process exits (driven by the ``quit`` command) or until ``timeout``
    seconds elapse.

    ``extra_args`` are appended after ``-b`` so callers can pass ``-C`` (no
    USB claim), ``-e`` (allow event processing), ``-R`` (reset device),
    ``-v`` (verbose), etc. Use ``run_mtp(extra_args=("-C",))``.

    ``retries_on_hang`` is the number of times to retry on
    ``subprocess.TimeoutExpired``. Each retry waits ``retry_sleep`` seconds
    to let the USB stack / IOKit cache settle before the next attempt.
    Non-zero exit codes are NOT retried — those mean "mtp-cli ran, refused".

    A timeout is normal when no Android device is plugged in or the device is
    not in MTP mode — ``mtp-cli`` will sit waiting on libusb for a device.
    Callers should check ``timed_out`` and report a friendly message instead
    of treating it as an error.
    """
    path = mtp_cli_path or find_mtp_cli()
    payload = "\n".join(commands) + "\nquit\n"
    argv = [str(path), "-b", *extra_args]
    last_command = commands[0] if commands else ""
    attempts_total = 0

    for attempt in range(retries_on_hang + 1):
        attempts_total += 1
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(f"mtp-cli not executable at {path}: {e}") from e

        try:
            stdout, stderr = proc.communicate(input=payload, timeout=timeout)
            return MTPResult(
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                exit_code=proc.returncode,
                attempts=attempts_total,
                last_command=last_command,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            if attempt >= retries_on_hang:
                return MTPResult(
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    exit_code=getattr(proc, "returncode", -1),
                    attempts=attempts_total,
                    last_command=last_command,
                )
            # Sleep then retry — the USB stack may need a moment to recover
            # from whatever the previous attempt left half-done.
            time.sleep(retry_sleep)

    # Unreachable: the loop either returns or retries. The final iteration
    # always returns via the timed_out branch. Keep this for type-checkers.
    raise RuntimeError("run_mtp: unreachable")


# -----------------------------------------------------------------------------
# Device detection with fallback flag combinations
# -----------------------------------------------------------------------------
#
# Real devices sometimes fail to enumerate under the default invocation. The
# combinations below cover the cases observed in the wild:
#
#   default  — what works in 99% of cases.
#   -C       — do not exclusively claim the USB interface. Useful when
#              another process (e.g. a leftover OpenMTP.app Helper) is
#              holding the interface even after the main app quit.
#   -e       — allow event processing. Untested upstream hypothesis from
#              openmtp-cli issue #1: a OnePlus 12 on macOS 13.6 reports
#              "kIOReturnNoDevice" (0xe00002be) under default invocation
#              but the GUI copy works. Worth retrying with events enabled.
#
# Order matters: ``detect_device`` stops on the first attempt that yields
# non-empty ``device-info`` or ``storage-list`` output. Don't put expensive
# / risky flags (e.g. ``-R`` which resets the USB device) in this default
# chain; gate those behind an explicit user opt-in.
#
MTP_DETECT_ATTEMPTS: list[tuple[str, tuple[str, ...]]] = [
    ("default", ()),
    ("-C",      ("-C",)),
    ("-e",      ("-e",)),
]


@dataclass
class DetectResult:
    """Outcome of a :func:`detect_device` run.

    Attributes:
        connected: True if any attempt returned non-empty ``device-info`` or
            ``storage-list`` output within the timeout.
        last_result: The raw :class:`MTPResult` from the last attempt that
            ran. Inspect ``last_result.stderr`` for IOKit / libusb errors.
        device_info: Parsed from the first successful attempt (or empty).
        storages: Parsed from the first successful attempt (or empty).
        attempted: Labels (in order) of every attempt that ran. The last
            entry corresponds to ``last_result``.
    """

    connected: bool
    last_result: MTPResult
    device_info: dict[str, str]
    storages: list[dict]
    attempted: list[str]


def detect_device(
    *,
    mtp_cli_path: Path,
    timeout: float = 5.0,
    commands: Sequence[str] = ("version", "device-info", "storage-list"),
    attempts: Sequence[tuple[str, tuple[str, ...]]] = MTP_DETECT_ATTEMPTS,
    runner: Optional[Any] = None,
    retries_on_hang: int = 0,
) -> DetectResult:
    """Try multiple mtp-cli flag combinations until one yields a device.

    Stops early on the first success or on the first timeout — timeouts are
    deterministic (no device on USB), so retrying with different flags does
    not help.

    ``retries_on_hang`` is forwarded to :func:`run_mtp` so each attempt is
    itself retried (with a sleep between attempts) when mtp-cli hangs.

    ``runner`` defaults to :func:`run_mtp`. Tests inject a fake runner that
    returns canned ``MTPResult`` values; production callers should leave it
    alone.
    """
    if not attempts:
        raise ValueError("attempts must not be empty")
    run = runner if runner is not None else run_mtp

    def _run_once(label: str, flags: tuple[str, ...]) -> tuple[str, MTPResult, dict, list]:
        result = run(
            list(commands),
            mtp_cli_path=mtp_cli_path,
            timeout=timeout,
            extra_args=list(flags),
            retries_on_hang=retries_on_hang,
        )
        return (
            label,
            result,
            parse_device_info(result.stdout),
            parse_storage_list(result.stdout),
        )

    label, last_result, device_info, storages = _run_once(*attempts[0])
    attempted = [label]
    connected = (not last_result.timed_out) and (bool(device_info) or bool(storages))

    for label, flags in attempts[1:]:
        if connected or last_result.timed_out:
            break
        attempted.append(label)
        label, last_result, device_info, storages = _run_once(label, flags)
        connected = bool(device_info) or bool(storages)

    return DetectResult(
        connected=connected,
        last_result=last_result,
        device_info=device_info,
        storages=storages,
        attempted=attempted,
    )


# -----------------------------------------------------------------------------
# MTP ObjectFormatCode → category / MIME
# -----------------------------------------------------------------------------
# Reference: libmtp.h ``MTP_FORMAT_*`` constants, used by ``android-file-transfer-linux``
# (the project that ships the ``mtp-cli`` binary bundled with OpenMTP.app).

MTP_FORMAT_UNDEFINED = 0x3000
MTP_FORMAT_ASSOCIATION = 0x3001  # folder
MTP_FORMAT_TEXT = 0x3004
MTP_FORMAT_HTML = 0x3005
MTP_FORMAT_XML = 0x300A
MTP_FORMAT_MSWORD = 0x300B
MTP_FORMAT_MSEXCEL = 0x300C
MTP_FORMAT_MSPPT = 0x300D

# Image range: 0x3800 - 0x38FF (per libmtp grouping)
# Video range: 0xB900 - 0xB9FF
# Audio range: 0xB200 - 0xB2FF

_MIME_BY_CODE: dict[int, str] = {
    MTP_FORMAT_ASSOCIATION: "inode/directory",
    MTP_FORMAT_TEXT: "text/plain",
    MTP_FORMAT_HTML: "text/html",
    MTP_FORMAT_XML: "application/xml",
    MTP_FORMAT_MSWORD: "application/msword",
    MTP_FORMAT_MSEXCEL: "application/vnd.ms-excel",
    MTP_FORMAT_MSPPT: "application/vnd.ms-powerpoint",
    0x3801: "image/jpeg",
    0x3802: "image/tiff",
    0x3804: "image/bmp",
    0x3807: "image/png",
    0x3808: "image/jp2",
    0x380B: "image/gif",
    0x380D: "image/jpeg",
    0x380E: "image/x-adobe-dng",
    0xB216: "audio/aac",
    0xB219: "audio/mpeg",
    0xB21C: "audio/x-wav",
    0xB901: "video/mp4",
    0xB982: "video/mpeg",
    0xB983: "video/quicktime",
    0xBA0A: "video/x-matroska",
}


def category_for_code(code: int) -> str:
    """Bucket an MTP ObjectFormatCode into one of: folder, image, video, audio, document, executable, other."""
    if code == MTP_FORMAT_ASSOCIATION:
        return "folder"
    if 0x3800 <= code <= 0x38FF:
        return "image"
    if 0xB900 <= code <= 0xB9FF:
        return "video"
    if 0xB200 <= code <= 0xB2FF:
        return "audio"
    if code == MTP_FORMAT_TEXT or code == MTP_FORMAT_HTML or code == MTP_FORMAT_XML:
        return "document"
    if 0x300B <= code <= 0x301F:
        return "document"  # Office, OpenDocument, etc.
    if code == 0x3003:
        return "executable"
    return "other"


def mime_for_code(code: int) -> str:
    """Best-effort MIME type for an ObjectFormatCode. Falls back to ``application/octet-stream``."""
    return _MIME_BY_CODE.get(code, "application/octet-stream")


_CATEGORY_ALIASES = {
    "image": "image",
    "images": "image",
    "photo": "image",
    "photos": "image",
    "video": "video",
    "videos": "video",
    "movie": "video",
    "movies": "video",
    "audio": "audio",
    "music": "audio",
    "sound": "audio",
    "document": "document",
    "documents": "document",
    "doc": "document",
    "docs": "document",
    "text": "document",
    "all": "all",
    "any": "all",
    "*": "all",
}


def normalize_filter(filter_value: str) -> str:
    """Map a free-form filter name (image / photos / mp4 / etc.) to a category.

    Raises ``ValueError`` on an unknown filter so the CLI can report it.
    """
    key = filter_value.strip().lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    # Try MIME type prefixes.
    if key.startswith("image/"):
        return "image"
    if key.startswith("video/"):
        return "video"
    if key.startswith("audio/"):
        return "audio"
    if key.startswith("text/") or key.startswith("application/"):
        return "document"
    raise ValueError(f"unknown --format-filter value: {filter_value!r}")


# -----------------------------------------------------------------------------
# Parsers for mtp-cli output
# -----------------------------------------------------------------------------
# These parsers are deliberately lenient: mtp-cli (android-file-transfer-linux)
# emits human-readable output whose exact column layout has changed across
# versions. We key off canonical substrings (``mtp object format =``,
# ``Storage:``, ``object id:``) rather than column positions.

_LSEXT_LINE_RE = re.compile(
    r"""
    ^[ \t]*
    object[ \t]id:[ \t]* (?P<oid>0x[0-9A-Fa-f]+) [ \t]*,?
    [ \t]* parent:[ \t]* (?P<parent>0x[0-9A-Fa-f]+) [ \t]*,?
    [ \t]* name:[ \t]* (?P<name>.+?)
    [ \t]* ,?[ \t]* type:[ \t]* (?P<type>folder|file)
    [ \t]* ,?[ \t]* size:[ \t]* (?P<size>\d+)
    [ \t]* ,?[ \t]* mtime:[ \t]* (?P<mtime>[^,\s]+)
    [ \t]* ,?[ \t]* mtp[ \t]+object[ \t]+format[ \t]*=[ \t]* (?P<fmt>0x[0-9A-Fa-f]+)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Some mtp-cli builds reorder fields. Fall back to a looser regex.
_LSEXT_LINE_LOOSE_RE = re.compile(
    r"object[ \t]+id:[ \t]+(?P<oid>0x[0-9A-Fa-f]+)", re.IGNORECASE
)

# The bundled mtp-cli v3.9-2 (android-file-transfer-linux) emits lsext /
# lsext-r output as FIXED-COLUMN positional values rather than the
# colon-prefixed form. Columns are whitespace-separated; the name is
# everything from the mtime timestamp to end of line:
#
#     <object_id>  <storage_id>  <format_hex>  <size>  <mtime>  <name>
#
# Example (verbatim from issue #2):
#     8          65537      3001          0 2026-09-24 18:52:03  Pictures
#
# mtime has a space (date + time), so we anchor the trailing fields.
_LSEXT_LINE_POSITIONAL_RE = re.compile(
    r"^\s*"
    r"(?P<oid>\d+)\s+"
    r"(?P<parent_storage>\d+)\s+"
    r"(?P<fmt>[0-9A-Fa-f]+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mtime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<name>.+?)\s*$"
)

_STORAGE_LINE_RE = re.compile(
    r"Storage:[ \t]*(?P<id>0x[0-9A-Fa-f]+)[ \t]+(?P<desc>.+?)(?:[ \t]+(?P<free>[\d.]+)\s*(?P<free_unit>[KMGT]?B)[ \t]+free[ \t]*/[ \t]*(?P<total>[\d.]+)\s*(?P<total_unit>[KMGT]?B)[ \t]+total)?\s*$",
    re.IGNORECASE,
)

# mtp-cli's `storage-list` (v3.9-2) actually emits:
#     65537    volume: , description: 内部共享存储空间
# where ``65537`` is the storage id in **decimal** (not hex), followed by
# ``volume: <size>, description: <name>`` after the spaces. This regex
# captures the positional form.
_STORAGE_LINE_POSITIONAL_RE = re.compile(
    r"^(?P<id>\d+)\s+volume:[ \t]*,?[ \t]*description:[ \t]*(?P<desc>.+?)\s*$",
    re.IGNORECASE,
)

_DEVICE_INFO_KEY_VALUE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z _]*?)[ \t]*:[ \t]*(?P<value>.+?)\s*$"
)

# Lines that look like libmtp / IOKit status output but carry no per-device
# info — skip them in both parse_device_info and parse_storage_list.
_NOISE_PREFIXES = (
    "IOCreatePlugInInterfaceForService",  # IOKit plugin-interface error
    "selected storage",                   # libmtp "selected storage <id>" status
)

# mtp-cli's `device-info` output, on the v3.9-2 (android-file-transfer-linux)
# build bundled with OpenMTP.app, is a FIXED-ORDER list of bare values rather
# than ``key: value`` lines:
#
#     OnePlus                       # manufacturer
#     PJD110                        # model
#     1.0                           # device version
#     E0F91C3176A64C078D2C01B9C94F975D  # serial number
#     microsoft.com: 1.0; android.com: 1.0;  # extended props (one per line)
#
# Older builds (and the help text in references/mtp-cli-commands.md) suggest
# a colon-separated ``Manufacturer: ...`` format that we ALSO accept. The
# positional fallback handles the bundled build; the key:value path handles
# anything else.
_DEVICE_INFO_POSITIONAL_KEYS = (
    "manufacturer",
    "model",
    "device version",
    "serial number",
)


@dataclass
class TreeNode:
    """One entry from a recursive directory listing."""

    object_id: int
    parent_id: int
    name: str
    is_dir: bool
    size: int
    mtime: str
    format_code: int
    mime: str
    category: str
    # Filled in by post-processing: the path relative to the tree root.
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "size": self.size,
            "mtime": self.mtime,
            "format_code": f"0x{self.format_code:04X}",
            "mime": self.mime,
            "category": self.category,
        }


def parse_lsext(output: str) -> list[TreeNode]:
    """Parse the output of an ``lsext`` or ``lsext-r`` command.

    Supports two formats:
      1. ``object id: 0x.., parent: 0x.., name: ..., type: folder|file,
         size: N, mtime: ..., mtp object format = 0x..`` (some mtp-cli
         builds / help-text examples).
      2. Fixed-column positional:
         ``<object_id>  <storage_id>  <format_hex>  <size>  <mtime>  <name>``
         (the v3.9-2 build bundled with OpenMTP.app).

    The parser returns nodes with object ids, sizes, and formats but
    without resolved paths. Callers should compose paths via
    :func:`attach_paths`.
    """
    nodes: list[TreeNode] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LSEXT_LINE_RE.match(line)
        if m:
            oid = int(m.group("oid"), 16)
            parent = int(m.group("parent"), 16)
            name = m.group("name").strip()
            is_dir = m.group("type").lower() == "folder"
            size = int(m.group("size"))
            mtime = m.group("mtime")
            fmt = int(m.group("fmt"), 16)
            nodes.append(
                TreeNode(
                    object_id=oid,
                    parent_id=parent,
                    name=name,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime,
                    format_code=fmt,
                    mime=mime_for_code(fmt),
                    category=category_for_code(fmt),
                )
            )
            continue

        m = _LSEXT_LINE_POSITIONAL_RE.match(line)
        if m:
            oid = int(m.group("oid"))          # decimal in this format
            parent_storage = int(m.group("parent_storage"))
            name = m.group("name").strip()
            fmt = int(m.group("fmt"), 16)      # bare hex, no 0x prefix
            size = int(m.group("size"))
            mtime = m.group("mtime")
            is_dir = (fmt == MTP_FORMAT_ASSOCIATION)
            nodes.append(
                TreeNode(
                    object_id=oid,
                    parent_id=parent_storage,  # we don't have a true parent_id here; storage_id is the closest
                    name=name,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime,
                    format_code=fmt,
                    mime=mime_for_code(fmt),
                    category=category_for_code(fmt),
                )
            )
            continue

        # Skip libmtp / IOKit noise lines; do not silently lose data
        # otherwise, but log if a hard test wants to assert the count.
        if _LSEXT_LINE_LOOSE_RE.search(line):
            continue
        if any(line.startswith(p) for p in _NOISE_PREFIXES):
            continue
        # Unrecognised non-empty line — drop silently (matches the old
        # behaviour; the caller can still inspect raw stdout via the
        # mtp-tree --no-json path or by piping).
    return nodes


def attach_paths(nodes: Iterable[TreeNode]) -> list[TreeNode]:
    """Set ``TreeNode.path`` by walking the parent/child graph.

    The first root (``parent_id == 0xFFFFFFFF``) is treated as the tree root and
    given path ``"."``. Other roots (shouldn't happen for a single ``lsext-r``
    call, but defensively) get path ``"<name>"``.
    """
    by_id: dict[int, TreeNode] = {n.object_id: n for n in nodes}
    # Find root(s).
    ROOT_PARENT = 0xFFFFFFFF
    roots = [n for n in nodes if n.parent_id == ROOT_PARENT]
    other_roots = [n for n in nodes if n.parent_id not in by_id and n.parent_id != ROOT_PARENT]

    # Build children index.
    children: dict[int, list[TreeNode]] = {}
    for n in nodes:
        children.setdefault(n.parent_id, []).append(n)

    result: list[TreeNode] = []

    def walk(node: TreeNode, path: str) -> None:
        node.path = path
        result.append(node)
        for child in children.get(node.object_id, []):
            walk(child, f"{path}/{child.name}")

    for root in roots:
        walk(root, root.name)
    for orphan in other_roots:
        walk(orphan, orphan.name)

    return result


def parse_storage_list(output: str) -> list[dict]:
    """Parse the output of ``storage-list`` into a list of storage records.

    Supports two output formats:
      1. ``Storage: <hex-id> <description> [<free> free / <total> total]``
         (some mtp-cli builds; ``Storage: <hex-id> <description>`` alone
         is also accepted).
      2. ``<decimal-id>    volume: <size>, description: <name>``
         (the v3.9-2 build bundled with OpenMTP.app uses this form).
    """
    records: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in _NOISE_PREFIXES):
            continue

        # Format 1: ``Storage:`` prefix.
        if line.lower().startswith("storage:"):
            m = _STORAGE_LINE_RE.match(line)
            if m:
                rec: dict = {
                    "id": m.group("id"),
                    "description": m.group("desc").strip(),
                }
                if m.group("free"):
                    rec["free"] = f"{m.group('free')} {m.group('free_unit')}"
                    rec["total"] = f"{m.group('total')} {m.group('total_unit')}"
                records.append(rec)
                continue
            # Fallback for `Storage: <id> <desc>` with no free/total.
            m2 = re.match(
                r"Storage:[ \t]*(?P<id>0x[0-9A-Fa-f]+|\d+)[ \t]+(?P<desc>.+)$",
                line,
                re.IGNORECASE,
            )
            if m2:
                records.append({
                    "id": m2.group("id"),
                    "description": m2.group("desc").strip(),
                })
                continue
            # Unrecognised Storage: line — preserve as raw for debugging.
            records.append({"raw": line, "parse_error": True})
            continue

        # Format 2: decimal-id + volume:/description:
        m3 = _STORAGE_LINE_POSITIONAL_RE.match(line)
        if m3:
            records.append({
                "id": m3.group("id"),
                "description": m3.group("desc").strip(),
            })
            continue

        # Otherwise: unknown / noise; skip silently.
    return records


def parse_device_info(output: str) -> dict[str, str]:
    """Parse the output of ``device-info`` into a flat key/value map.

    Supports two output formats:
      1. ``key: value`` lines (some mtp-cli builds)
      2. Bare values in fixed order: manufacturer, model, device version,
         serial number, then one or more extended-property lines
         (the v3.9-2 build bundled with OpenMTP.app uses this form).

    Key:value lines take precedence over the positional fallback: if a line
    matches both, the explicit key wins. Lines that look like libmtp /
    IOKit noise are skipped.
    """
    info: dict[str, str] = {}
    positional_idx = 0
    extra_idx = 0

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in _NOISE_PREFIXES):
            continue
        if line.lower().startswith("storage:"):
            continue
        # Skip the bare-numeric "65537    volume:..." form — that's
        # storage-list territory, handled by parse_storage_list.
        if _STORAGE_LINE_POSITIONAL_RE.match(line):
            continue

        m = _DEVICE_INFO_KEY_VALUE_RE.match(line)
        if m:
            info[m.group("key").strip().lower()] = m.group("value").strip()
            continue

        # Positional fallback for the bare-value format. Find the first
        # empty positional slot (an explicit key:value line may have
        # already filled slot 0, in which case the first bare value goes
        # into slot 1, etc.). Once all positional slots are full, treat
        # the line as an extended property.
        filled = False
        for k in range(positional_idx, len(_DEVICE_INFO_POSITIONAL_KEYS)):
            key = _DEVICE_INFO_POSITIONAL_KEYS[k]
            if key not in info:
                info[key] = line
                positional_idx = k + 1
                filled = True
                break
        if not filled and positional_idx >= len(_DEVICE_INFO_POSITIONAL_KEYS):
            extra_idx += 1
            info[f"extended property {extra_idx}"] = line

    return info


# -----------------------------------------------------------------------------
# I/O helpers used by mtp-backup
# -----------------------------------------------------------------------------

def filter_nodes(
    nodes: Sequence[TreeNode],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    format_filter: Optional[str] = None,
) -> list[TreeNode]:
    """Return the subset of ``nodes`` matching include/exclude/category filters.

    Filtering only applies to file nodes; folder nodes are kept when they have
    at least one matching descendant so the local mirror preserves directory
    structure. Folders with no matching descendants are dropped.
    """
    import fnmatch

    target_category = normalize_filter(format_filter) if format_filter else None

    def keep(node: TreeNode) -> bool:
        if target_category and target_category != "all":
            if not node.is_dir and node.category != target_category:
                return False
        if include and not node.is_dir:
            if not any(fnmatch.fnmatch(node.name, pat) for pat in include):
                return False
        if exclude and not node.is_dir:
            if any(fnmatch.fnmatch(node.name, pat) for pat in exclude):
                return False
        return True

    # First pass: file-level filter.
    file_passes = {n.object_id: keep(n) for n in nodes if not n.is_dir}

    # Second pass: keep a folder iff any of its descendants (recursively) passed.
    folder_passes: dict[int, bool] = {}
    children_by_parent: dict[int, list[TreeNode]] = {}
    for n in nodes:
        if n.is_dir:
            children_by_parent.setdefault(n.parent_id, []).append(n)
        else:
            children_by_parent.setdefault(n.parent_id, []).append(n)

    def folder_keeps_descendant(node: TreeNode) -> bool:
        for child in children_by_parent.get(node.object_id, []):
            if child.is_dir:
                if folder_keeps_descendant(child):
                    return True
            elif file_passes.get(child.object_id, False):
                return True
        return False

    for n in nodes:
        if n.is_dir:
            folder_passes[n.object_id] = folder_keeps_descendant(n)

    return [
        n for n in nodes
        if (n.is_dir and folder_passes.get(n.object_id, False))
        or (not n.is_dir and file_passes.get(n.object_id, False))
    ]


# -----------------------------------------------------------------------------
# Persistent mtp-cli session
# -----------------------------------------------------------------------------
#
# Two failure modes make "one process per command" unusable on OnePlus 12:
#
#   1. Every mtp-cli start claims the USB interface and every exit releases
#      it. After a handful of claim/release cycles the phone's MTP daemon
#      wedges and even `device-info` hangs (see issue #2).
#   2. Large directory listings (2000+ entries) blow past the stdout pipe
#      buffer before a short communicate()-style reader gets going.
#
# MTPSession solves both: ONE mtp-cli process lives for the whole walk,
# commands are fed one at a time over stdin, and output is streamed with a
# "quiet gap" heuristic to detect command completion (mtp-cli batch mode
# emits no completion marker between commands).


class MTPSession:
    """A single long-lived mtp-cli batch-mode process.

    Usage::

        with MTPSession(mtp_cli_path) as sess:
            lines = sess.run_command("lsext /DCIM", timeout=20)
            more = sess.run_command("lsext /DCIM/Camera", timeout=60)
        # close() kills the process; mtp-cli's `quit` does not reliably exit.
    """

    def __init__(
        self,
        mtp_cli_path: Path,
        *,
        extra_args: Sequence[str] = (),
        quiet_gap: float = 1.0,
        connect_timeout: float = 15.0,
    ):
        self.path = mtp_cli_path
        self.quiet_gap = quiet_gap
        self.connect_timeout = connect_timeout
        # stderr goes to a temp file: get/put transfers can emit IOKit noise
        # on stderr, and we want it available for diagnostics without risking
        # a PIPE-buffer deadlock or polluting the stdout stream we parse.
        self._stderr_file = tempfile.TemporaryFile(mode="w+")
        self.proc = subprocess.Popen(
            [str(mtp_cli_path), "-b", *extra_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            bufsize=1,
        )
        self._sel = selectors.DefaultSelector()
        self._sel.register(self.proc.stdout, selectors.EVENT_READ)
        # Drain the session banner ("selected storage ..."), waiting for the
        # first quiet gap — same heuristic as run_command.
        self._banner_lines = self._read_until_quiet(connect_timeout)

    def __enter__(self) -> "MTPSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_until_quiet(self, timeout: float) -> list[str]:
        """Stream stdout lines until no new data arrives for ``quiet_gap``
        seconds or ``timeout`` elapses."""
        lines: list[str] = []
        last_data: Optional[float] = None
        deadline = time.monotonic() + timeout
        while True:
            events = self._sel.select(0.1)
            now = time.monotonic()
            if events:
                for key, _ in events:
                    line = key.fileobj.readline()
                    if line:
                        lines.append(line.rstrip("\n"))
                        last_data = now
                if now > deadline:
                    break
            else:
                if last_data and (now - last_data) > self.quiet_gap:
                    break
                if not last_data and now > deadline:
                    break
        return lines

    def run_command(self, command: str, *, timeout: float = 20.0) -> list[str]:
        """Send one command, return its stdout lines.

        Raises ``TimeoutError`` if the command produced no output at all
        within ``timeout`` (device gone / MTP daemon wedged). A command that
        produced *some* output before going quiet returns what it got.
        """
        if self.proc.poll() is not None:
            raise RuntimeError(f"mtp-cli session exited early (code {self.proc.returncode})")
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        lines = self._read_until_quiet(timeout)
        if not lines and self.proc.poll() is not None:
            raise RuntimeError(f"mtp-cli died while running {command!r}")
        return lines

    def run_transfer(
        self,
        command: str,
        *,
        timeout: float = 120.0,
        probe: str = "pwd",
    ) -> list[str]:
        """Run a silent command (``get`` / ``put``) and detect completion.

        Transfers produce no stdout, so the quiet-gap heuristic cannot tell
        "still transferring" from "finished". Instead we queue a probe
        command (``pwd`` by default) right after the transfer command: mtp-cli
        processes stdin strictly in order, so the FIRST stdout line received
        after queuing is the probe's response — meaning the transfer finished.

        Returns the probe's output lines (empty strings filtered out) plus
        anything the transfer itself printed (usually nothing, or an error).

        Raises ``TimeoutError`` if the probe never responds within
        ``timeout`` seconds — the transfer (or the session) is stuck; the
        caller should abandon this session.
        """
        if self.proc.poll() is not None:
            raise RuntimeError(f"mtp-cli session exited early (code {self.proc.returncode})")
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.write(probe + "\n")
        self.proc.stdin.flush()

        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self._sel.select(0.2)
            for key, _ in events:
                line = key.fileobj.readline()
                if line and line.strip():
                    lines.append(line.rstrip("\n"))
            if lines:
                # First non-empty output = the probe responded → done.
                return lines
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"mtp-cli died (code {self.proc.returncode}) while running {command!r}"
                )
        raise TimeoutError(
            f"{command.split()[0] if command.split() else command} did not complete "
            f"within {timeout}s (probe {probe!r} never responded)"
        )

    def stderr_tail(self, chars: int = 2000) -> str:
        """Return the last ``chars`` characters written to stderr so far."""
        try:
            self._stderr_file.flush()
            self._stderr_file.seek(0, 2)  # seek end
            size = self._stderr_file.tell()
            self._stderr_file.seek(max(0, size - chars))
            return self._stderr_file.read()
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write("quit\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
        finally:
            self._sel.close()
            try:
                self._stderr_file.close()
            except (OSError, ValueError):
                pass


# -----------------------------------------------------------------------------
# Tree walk (non-recursive workaround)
# -----------------------------------------------------------------------------
#
# On OnePlus 12 (and likely other OPPO-derived devices), ``lsext-r <path>``
# (and ``ls -r``) hang indefinitely. The device returns the first few
# entries on stdout, then mtp-cli sits waiting for the next GetObjectHandles
# response that never comes — eventually leaving the USB interface in a
# half-claimed state that breaks every subsequent mtp-cli invocation until
# ``sudo killall -HUP usbd`` resets the macOS USB daemon. Reproduced on
# macOS 13.6 with the mtp-cli v3.9-2 build bundled with OpenMTP.app 3.3.0;
# documented in issue #2.
#
# The workaround is to drive the recursion from the wrapper, calling the
# KNOWN-WORKING non-recursive ``lsext <path>`` once per directory. Each
# invocation is small enough that the OnePlus MTP state machine can answer
# without hanging. Total wall time scales with directory count
# (≈ 1 subprocess + 1 mtp-cli startup per subdirectory) but is bounded
# and predictable. Pass ``recursive=True`` to fall back to ``lsext-r`` (fast
# on devices that work; hangs on OnePlus).
#
# The returned :class:`TreeNode` list has ``path`` set to a slash-separated
# path RELATIVE TO THE DEVICE ROOT, matching what :func:`attach_paths` does
# for a single ``lsext-r`` call. Callers can feed the list straight to
# ``json.dumps`` after ``to_dict()``-ing each node.

import fnmatch as _fnmatch


def walk_tree(
    path: str,
    *,
    mtp_cli_path: Path,
    timeout: float,
    retries_on_hang: int = 0,
    max_depth: Optional[int] = None,
    format_filter: Optional[str] = None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    recursive: bool = False,
    run_fn: Optional[Any] = None,
    session_fn: Optional[Any] = None,
) -> tuple[list[TreeNode], list[str]]:
    """Walk a directory tree.

    Three modes, in order of preference:

    1. **Persistent session DFS** (default): one :class:`MTPSession` process
       for the entire walk, one non-recursive ``lsext <path>`` command per
       directory. This avoids both OnePlus failure modes (USB claim/release
       cycles wedging the MTP daemon, and pipe-buffer deadlock on large
       listings). Override in tests via ``session_fn``.
    2. ``recursive=True``: single ``lsext-r <path>`` call (fast on devices
       where native recursion works; hangs on OnePlus). Partial stdout is
       salvaged on timeout.
    3. ``run_fn`` provided (tests): per-directory separate processes via the
       injected runner. Production code should not use this mode.

    Returns ``(nodes, errors)``: a pre-order list of :class:`TreeNode` with
    ``path`` populated, plus human-readable warnings for subdirectories
    that errored during the walk.
    """
    errors: list[str] = []

    # Mode 2: single recursive call.
    if recursive:
        cmd = ["lsext-r"] if path in ("", "/") else [f"lsext-r {path}"]
        result = run_mtp(
            cmd, mtp_cli_path=mtp_cli_path, timeout=timeout,
            retries_on_hang=retries_on_hang,
        )
        if result.timed_out:
            partial = parse_lsext(result.stdout)
            if partial:
                attached = attach_paths(partial)
                return (
                    _apply_filters(attached, max_depth=max_depth, format_filter=format_filter,
                                   include=include, exclude=exclude),
                    [f"lsext-r {path} timed out after {timeout}s; partial result returned"],
                )
            errors.append(f"lsext-r {path} timed out after {timeout}s (no partial output)")
            return [], errors
        nodes = parse_lsext(result.stdout)
        if not nodes:
            errors.append(f"lsext-r {path} returned no entries")
            return [], errors
        attached = attach_paths(nodes)
        return (
            _apply_filters(attached, max_depth=max_depth, format_filter=format_filter,
                           include=include, exclude=exclude),
            errors,
        )

    all_nodes: list[TreeNode] = []

    def _list_children(current_path: str, per_dir_timeout: float) -> Optional[list[TreeNode]]:
        """List one directory. Returns parsed children, or None on error
        (the error is appended to ``errors``)."""
        cmd = "lsext" if current_path in ("", "/") else f"lsext {current_path}"
        label = f"lsext {current_path}" if current_path not in ("", "/") else "lsext /"
        try:
            lines = session.run_command(cmd, timeout=per_dir_timeout)
        except TimeoutError:
            errors.append(f"{label} produced no output within {per_dir_timeout}s")
            return None
        except (RuntimeError, OSError) as e:
            errors.append(f"{label} failed: {e}")
            return None
        children = parse_lsext("\n".join(lines))
        # Drop any echoed root marker defensively.
        return [n for n in children if n.parent_id != 0xFFFFFFFF]

    def _walk(current_path: str, current_depth: int) -> None:
        children = _list_children(current_path, timeout)
        if children is None:
            return
        for n in children:
            n.path = n.name if current_path in ("", "/") else f"{current_path}/{n.name}"
        all_nodes.extend(children)
        if max_depth is None or max_depth > current_depth + 1:
            for n in children:
                if n.is_dir:
                    _walk(n.path, current_depth + 1)

    # Mode 3 (tests): per-directory separate processes via injected runner.
    if run_fn is not None:
        run = run_fn

        def _walk_runfn(current_path: str, current_depth: int) -> None:
            cmd = ["lsext"] if current_path in ("", "/") else [f"lsext {current_path}"]
            result = run(cmd, mtp_cli_path=mtp_cli_path, timeout=timeout,
                         retries_on_hang=retries_on_hang)
            if result.timed_out:
                errors.append(f"lsext {current_path} timed out after {timeout}s")
                return
            children = parse_lsext(result.stdout)
            children = [n for n in children if n.parent_id != 0xFFFFFFFF]
            for n in children:
                n.path = n.name if current_path in ("", "/") else f"{current_path}/{n.name}"
            all_nodes.extend(children)
            if max_depth is None or max_depth > current_depth + 1:
                for n in children:
                    if n.is_dir:
                        _walk_runfn(n.path, current_depth + 1)

        _walk_runfn(path, 0)
        return _apply_filters(all_nodes, max_depth=None, format_filter=format_filter,
                              include=include, exclude=exclude), errors

    # Mode 1 (default): persistent session DFS.
    session_ctx = session_fn if session_fn is not None else MTPSession
    with session_ctx(mtp_cli_path) as session:
        _walk(path, 0)

    return _apply_filters(all_nodes, max_depth=None, format_filter=format_filter,
                          include=include, exclude=exclude), errors


def _apply_filters(
    nodes: Sequence[TreeNode],
    *,
    max_depth: Optional[int],
    format_filter: Optional[str],
    include: Sequence[str],
    exclude: Sequence[str],
) -> list[TreeNode]:
    """Depth + include/exclude/category filter, applied to a flat node list.

    depth_of(path) counts the segments between slashes; root is depth 1.
    max_depth is the *inclusive* max (--depth 1 = root only, --depth 2 =
    root + its immediate children).
    """
    if not nodes:
        return []

    # Depth filter first.
    if max_depth is not None:
        kept: list[TreeNode] = []
        for n in nodes:
            d = 0 if n.path == "" else n.path.count("/") + 1
            if d <= max_depth:
                kept.append(n)
        nodes = kept

    # Include/exclude (file-level).
    if include or exclude:
        out: list[TreeNode] = []
        for n in nodes:
            if n.is_dir:
                out.append(n)
                continue
            if include and not any(_fnmatch.fnmatch(n.name, p) for p in include):
                continue
            if exclude and any(_fnmatch.fnmatch(n.name, p) for p in exclude):
                continue
            out.append(n)
        nodes = out

    # Format filter — keep matching files; keep folders that contain
    # at least one matching descendant.
    if format_filter and format_filter != "all":
        target = category_for_code_to_category(format_filter)
        if target != "all":
            children_by_parent: dict[int, list[TreeNode]] = {}
            for n in nodes:
                children_by_parent.setdefault(n.parent_id, []).append(n)

            def file_matches(n: TreeNode) -> bool:
                return (not n.is_dir) and _node_category(n) == target

            def folder_has_match(n: TreeNode) -> bool:
                for child in children_by_parent.get(n.object_id, []):
                    if child.is_dir:
                        if folder_has_match(child):
                            return True
                    elif file_matches(child):
                        return True
                return False

            nodes = [n for n in nodes
                     if (not n.is_dir and file_matches(n))
                     or (n.is_dir and folder_has_match(n))]

    return nodes


def _node_category(n: TreeNode) -> str:
    """Category for a TreeNode, computed from its format_code."""
    return category_for_code(n.format_code)


def category_for_code_to_category(value: str) -> str:
    """Normalize a --format-filter string to a category name.

    Exposed as a separate function so tests can import it without depending
    on the unexported ``_normalize_filter``.
    """
    return normalize_filter(value)

def _fmt_bytes(n: int) -> str:
    """Human-readable byte count (1.2 MB, 3.4 GB, ...)."""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(f) < 1024.0:
            return f"{f:3.1f} {unit}" if unit != "B" else f"{int(f)} {unit}"
        f /= 1024.0
    return f"{f:.1f} PB"


# -----------------------------------------------------------------------------
# JSON output helpers
# -----------------------------------------------------------------------------

def emit_json(obj, *, indent: Optional[int] = 2) -> None:
    """Print ``obj`` as JSON to stdout. Errors go to stderr (caller's job)."""
    json.dump(obj, sys.stdout, indent=indent, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


def die(message: str, *, code: int = 1) -> "NoReturn":
    """Print ``Error: <message>`` to stderr and exit with ``code``."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)