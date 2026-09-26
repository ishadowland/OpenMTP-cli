#!/usr/bin/env python3
"""mtp-backup — selectively copy files from a connected MTP device to a local
directory.

Filtering knobs:
  --include GLOB          repeat to allow-list (default: everything)
  --exclude GLOB          repeat to deny-list (applied after include)
  --format-filter TYPE    image|video|audio|document|all

Safety:
  --dry-run / --no-dry-run
        Default is --dry-run. Without --no-dry-run, no files are copied; the
        output JSON shows what WOULD be copied. MTP operations on a phone
        are awkward to undo — make this explicit.

  --overwrite / --no-overwrite
        Default is --no-overwrite: if the destination file already exists, the
        file is skipped. Pass --overwrite to clobber it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _openmtp as om  # noqa: E402


def plan(
    tree_nodes: list[om.TreeNode],
    *,
    include: list[str],
    exclude: list[str],
    format_filter: str | None,
) -> list[om.TreeNode]:
    """Filter the tree down to files that should be copied, preserving folder structure."""
    return om.filter_nodes(
        tree_nodes,
        include=include,
        exclude=exclude,
        format_filter=format_filter,
    )


def make_local_dirs(
    planned_nodes: list[om.TreeNode],
    src_root: str,
    dst_root: Path,
) -> list[dict]:
    """Create the local mirror directories and return a list of error records (usually empty)."""
    errors: list[dict] = []
    seen: set[str] = set()
    for n in planned_nodes:
        if not n.is_dir:
            continue
        rel = _rel_from_root(n.path, src_root)
        local_dir = dst_root / rel
        if local_dir.as_posix() in seen:
            continue
        seen.add(local_dir.as_posix())
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            errors.append({"path": str(local_dir), "error": str(e)})
    return errors


def _rel_from_root(path: str, src_root: str) -> str:
    """Compute the destination-relative path for a node in the tree.

    ``src_root`` is the user-supplied ``--src`` argument (e.g. ``/DCIM/Camera``).
    Tree paths are computed relative to the tree root (``/``), so we need to
    strip the leading ``src_root`` prefix to land at a clean relative path
    under ``dst_root``.
    """
    if src_root in ("", "/"):
        return path if path != "." else ""
    src_root = src_root.lstrip("/")
    p = path.lstrip("/")
    if p == src_root:
        return ""
    if p.startswith(src_root + "/"):
        return p[len(src_root) + 1:]
    return p  # fallback — tree may not have started at src_root exactly


def copy_one(
    *,
    src_path: str,
    local_dst: Path,
    mtp_cli_path: Path,
    overwrite: bool,
    timeout: float,
) -> dict:
    """Run a single ``mtp-cli get`` to copy ``src_path`` to ``local_dst``.

    Returns a result dict suitable for the ``copied`` / ``skipped`` / ``errors``
    list in the final JSON.
    """
    if local_dst.exists() and not overwrite:
        return {"src": src_path, "dst": str(local_dst), "skipped": True, "reason": "exists"}

    local_dst.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = om.run_mtp(
        [f"get {src_path} {local_dst}"],
        mtp_cli_path=mtp_cli_path,
        timeout=timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if result.timed_out:
        return {"src": src_path, "dst": str(local_dst), "error": f"mtp-cli timed out after {timeout}s"}
    if result.exit_code != 0:
        return {
            "src": src_path,
            "dst": str(local_dst),
            "error": f"mtp-cli exit {result.exit_code}",
            "stderr": result.stderr.strip()[-500:],
        }
    if not local_dst.exists():
        return {
            "src": src_path,
            "dst": str(local_dst),
            "error": "mtp-cli reported success but file is not on disk",
        }
    return {
        "src": src_path,
        "dst": str(local_dst),
        "size": local_dst.stat().st_size,
        "took_ms": elapsed_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtp-backup",
        description="Selectively copy files from a connected MTP device to a local directory.",
    )
    parser.add_argument("--src", required=True, help="Source directory on the device.")
    parser.add_argument("--dst", required=True, help="Local destination directory (created if missing).")
    parser.add_argument("--include", action="append", default=[], help="Glob to allow (repeatable).")
    parser.add_argument("--exclude", action="append", default=[], help="Glob to deny (repeatable, applied after include).")
    parser.add_argument(
        "--format-filter",
        choices=["image", "video", "audio", "document", "all"],
        default=None,
        help="Restrict to a single category.",
    )
    parser.add_argument("--depth", type=int, default=None, help="Max recursion depth (passed to mtp-tree).")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="(default) Plan only — do not copy. Use --no-dry-run to actually copy.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Execute the copy. Required for any files to be written.",
    )
    parser.add_argument(
        "--overwrite",
        dest="overwrite",
        action="store_true",
        default=False,
        help="Overwrite destination files that already exist. Default: skip.",
    )
    parser.add_argument("--mtp-cli-path", help="Override path to the mtp-cli binary.")
    parser.add_argument(
        "--list-timeout",
        type=float,
        default=10.0,
        help="Seconds for the initial directory listing (default: 10).",
    )
    parser.add_argument(
        "--file-timeout",
        type=float,
        default=60.0,
        help="Seconds per file transfer (default: 60). Large videos need more.",
    )
    parser.add_argument(
        "--retry-on-hang",
        type=int,
        default=0,
        metavar="N",
        help="If mtp-cli hangs on either the listing or a per-file "
             "transfer, kill it, wait, and retry up to N times before giving "
             "up. Default: 0.",
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

    # Step 1: list
    commands = ["lsext-r"] if args.src in ("/", "") else [f"lsext-r {args.src}"]
    list_result = om.run_mtp(
        commands,
        mtp_cli_path=mtp_cli_path,
        timeout=args.list_timeout,
        retries_on_hang=max(0, args.retry_on_hang),
    )
    if list_result.timed_out:
        om.die(
            f"mtp-cli did not respond within {args.list_timeout}s while listing "
            f"{args.src!r} after {list_result.attempts} attempt(s) "
            f"(last_command={list_result.last_command!r}).\n"
            + om.hang_recovery_hint(list_result),
            code=2,
        )
    nodes = om.parse_lsext(list_result.stdout)
    if not nodes:
        om.die(f"no entries returned for {args.src!r}.", code=2)

    nodes = om.attach_paths(nodes)
    planned = plan(
        nodes,
        include=args.include,
        exclude=args.exclude,
        format_filter=args.format_filter,
    )

    dst_root = Path(args.dst).expanduser().resolve()
    mkdir_errors = make_local_dirs(planned, args.src, dst_root)

    # Collect file ops + per-file copy records.
    file_nodes = [n for n in planned if not n.is_dir]
    file_plan = [
        {
            "src": ("/" + n.path) if not n.path.startswith("/") else n.path,
            "dst": str(dst_root / _rel_from_root(n.path, args.src) / n.name),
            "size": n.size,
            "category": n.category,
            "mime": n.mime,
        }
        for n in file_nodes
    ]

    copied: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = list(mkdir_errors)

    if args.dry_run:
        # Surface every planned file under "planned" instead of "copied".
        result = {
            "dry_run": True,
            "src": args.src,
            "dst": str(dst_root),
            "include": args.include,
            "exclude": args.exclude,
            "format_filter": args.format_filter,
            "retry_on_hang": args.retry_on_hang,
            "planned": file_plan,
            "copied": [],
            "skipped": [],
            "errors": errors,
        }
    else:
        for entry in file_plan:
            r = copy_one(
                src_path=entry["src"],
                local_dst=Path(entry["dst"]),
                mtp_cli_path=mtp_cli_path,
                overwrite=args.overwrite,
                timeout=args.file_timeout,
            )
            if "skipped" in r:
                skipped.append(r)
            elif "error" in r:
                errors.append(r)
            else:
                copied.append(r)
        result = {
            "dry_run": False,
            "src": args.src,
            "dst": str(dst_root),
            "include": args.include,
            "exclude": args.exclude,
            "format_filter": args.format_filter,
            "retry_on_hang": args.retry_on_hang,
            "planned": file_plan,
            "copied": copied,
            "skipped": skipped,
            "errors": errors,
        }

    if args.no_json:
        mode = "DRY RUN" if args.dry_run else "COPY"
        print(f"[{mode}] {args.src} -> {dst_root}")
        print(f"  planned: {len(file_plan)} file(s)")
        if not args.dry_run:
            print(f"  copied:  {len(copied)}")
            print(f"  skipped: {len(skipped)}")
            print(f"  errors:  {len(errors)}")
        if errors:
            for e in errors:
                print(f"    ! {e.get('src', e.get('path'))}: {e.get('error')}")
        return 0 if not errors else 2

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())