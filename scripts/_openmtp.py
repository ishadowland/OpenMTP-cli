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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, NoReturn, Optional, Sequence


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
    """Result of one batch-mode mtp-cli invocation."""

    stdout: str
    stderr: str
    timed_out: bool
    exit_code: int


def run_mtp(
    commands: Sequence[str],
    *,
    mtp_cli_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> MTPResult:
    """Spawn mtp-cli in batch mode and run a sequence of commands.

    ``commands`` is joined with newlines, a trailing ``quit`` is appended, and
    the whole payload is fed to mtp-cli on stdin. Output is read until the
    process exits (driven by the ``quit`` command) or until ``timeout``
    seconds elapse.

    A timeout is normal when no Android device is plugged in or the device is
    not in MTP mode — ``mtp-cli`` will sit waiting on libusb for a device.
    Callers should check ``timed_out`` and report a friendly message instead
    of treating it as an error.
    """
    path = mtp_cli_path or find_mtp_cli()
    payload = "\n".join(commands) + "\nquit\n"

    try:
        proc = subprocess.Popen(
            [str(path), "-b"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise FileNotFoundError(f"mtp-cli not executable at {path}: {e}") from e

    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
        return MTPResult(stdout=stdout, stderr=stderr, timed_out=False, exit_code=proc.returncode)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return MTPResult(stdout=stdout, stderr=stderr, timed_out=True, exit_code=proc.returncode)


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

_STORAGE_LINE_RE = re.compile(
    r"Storage:[ \t]*(?P<id>0x[0-9A-Fa-f]+)[ \t]+(?P<desc>.+?)(?:[ \t]+(?P<free>[\d.]+)\s*(?P<free_unit>[KMGT]?B)[ \t]+free[ \t]*/[ \t]*(?P<total>[\d.]+)\s*(?P<total_unit>[KMGT]?B)[ \t]+total)?\s*$",
    re.IGNORECASE,
)

_DEVICE_INFO_KEY_VALUE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z _]*?)[ \t]*:[ \t]*(?P<value>.+?)\s*$"
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

    The parser returns nodes with object ids, sizes, and formats but without
    resolved paths. Callers should compose paths via :func:`attach_paths`.
    """
    nodes: list[TreeNode] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LSEXT_LINE_RE.match(line)
        if not m:
            # Try the loose matcher to skip header/footer lines cleanly.
            if not _LSEXT_LINE_LOOSE_RE.search(line):
                continue
            continue
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
    """Parse the output of ``storage-list`` into a list of storage records."""
    records: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.lower().startswith("storage:"):
            continue
        m = _STORAGE_LINE_RE.match(line)
        if not m:
            # Keep what we can; flag the parse failure for the caller.
            records.append({"raw": line, "parse_error": True})
            continue
        rec: dict = {
            "id": m.group("id"),
            "description": m.group("desc").strip(),
        }
        if m.group("free"):
            rec["free"] = f"{m.group('free')} {m.group('free_unit')}"
            rec["total"] = f"{m.group('total')} {m.group('total_unit')}"
        records.append(rec)
    return records


def parse_device_info(output: str) -> dict[str, str]:
    """Parse the output of ``device-info`` into a flat key/value map."""
    info: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        m = _DEVICE_INFO_KEY_VALUE_RE.match(line)
        if m:
            info[m.group("key").strip().lower()] = m.group("value").strip()
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
# Misc formatting
# -----------------------------------------------------------------------------

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