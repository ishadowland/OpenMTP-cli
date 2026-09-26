# openmtp-cli

> Command-line interface for OpenMTP — the macOS Android MTP file transfer app.
> Designed to be called by **Hermes** and **OpenClaw** agents (or any LLM
> tool runner) as a small set of scripts that wrap the `mtp-cli` binary
> bundled with [OpenMTP.app](https://github.com/ganeshrvel/openmtp).

## What this is

Three Python 3 scripts that drive `mtp-cli` in batch mode:

| Script | Purpose |
| --- | --- |
| `scripts/mtp_list.py` | Detect a connected Android device and list its storage volumes. |
| `scripts/mtp_tree.py` | Recursively list a directory on the device, annotated with file-type metadata (MTP `ObjectFormatCode` → category + MIME). |
| `scripts/mtp_backup.py` | Selectively copy files from a device to a local directory, with `include` / `exclude` / `--format-filter` knobs. Defaults to **dry-run** — explicit `--no-dry-run` to actually copy. |

All three:

- are Python 3 standard-library only (no `pip install`)
- print JSON to stdout (or human text with `--no-json`)
- exit non-zero with `Error: ...` on stderr on failure
- auto-discover the `mtp-cli` binary at
  `/Volumes/OpenMTP*/OpenMTP.app/Contents/Resources/bin/mtp-cli`
  (override via `--mtp-cli-path` or `$OPENMTP_CLI`)
- enforce a timeout per `mtp-cli` call so a stuck USB claim can't hang the
  caller

## Why this exists

OpenMTP.app is fast (much faster than macOS's stock Photos / Image Capture
MTP stack, which scans the whole device on every connect), but a few jobs
are awkward from the GUI:

- selecting 5,000 photos by hand
- running an incremental mirror of `DCIM/` to a NAS on a schedule
- scripting around the queue

The CLI makes those scriptable. The scripts are deliberately thin: every
interesting decision (which files to back up, where to put them, what
counts as a "match") is exposed as a flag so a wrapping agent doesn't need
to second-guess defaults.

## Requirements

- macOS (the upstream OpenMTP.app is macOS-only)
- OpenMTP.app 3.3.0+ installed and launched at least once so its bundle is
  mounted at `/Volumes/OpenMTP*/`
- Python 3.10 or newer

No third-party packages.

## Install

Clone the repo, then either:

- run the scripts directly from the clone (`python3 scripts/mtp_list.py`),
  or
- symlink them onto your `PATH`:

  ```sh
  ln -s "$(pwd)/scripts/mtp_list.py"   ~/.local/bin/mtp-list
  ln -s "$(pwd)/scripts/mtp_tree.py"   ~/.local/bin/mtp-tree
  ln -s "$(pwd)/scripts/mtp_backup.py" ~/.local/bin/mtp-backup
  ```

For an **agent skill** (Hermes / OpenClaw), use the companion repo
[`ishadowland/openmtp-skill`](https://github.com/ishadowland/openmtp-skill)
which bundles a SKILL.md and an `install.sh` that wires both targets at
once.

## Usage

### `mtp-list`

```sh
$ mtp-list
{
  "connected": true,
  "mtp_cli_path": "/Volumes/OpenMTP 3.3.0/OpenMTP.app/Contents/Resources/bin/mtp-cli",
  "device": {
    "manufacturer": "Google",
    "model": "Pixel 8",
    "device version": "14",
    "serial number": "ABC123"
  },
  "storages": [
    {
      "id": "0x00010001",
      "description": "Phone storage",
      "free": "123.45 GB",
      "total": "256.00 GB"
    }
  ]
}
```

No device? `connected` is `false`, the script exits `1`. The `--no-json`
flag prints a human-readable summary instead.

### `mtp-tree`

```sh
$ mtp-tree --path /DCIM --format-filter image --no-json
DCIM/  (0x00000001)
DCIM/Camera/  (0x00000002)
DCIM/Camera/IMG_001.jpg  [image] 5.0 MB  (image/jpeg, 0x3801)
DCIM/Camera/IMG_002.jpg  [image] 6.0 MB  (image/jpeg, 0x3801)
DCIM/Camera/VID_001.mp4  [video] 100.0 MB  (video/mp4, 0xb901)
```

With `--json` (default), each row becomes:

```json
{
  "path": "DCIM/Camera/IMG_001.jpg",
  "name": "IMG_001.jpg",
  "is_dir": false,
  "size": 5242880,
  "mtime": "2024-09-01T10:30:00Z",
  "format_code": "0x3801",
  "mime": "image/jpeg",
  "category": "image"
}
```

Flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--path` | `/` | Directory on the device. |
| `--depth` | unbounded | 1 = root only, 2 = root + immediate children, ... |
| `--format-filter` | none | `image` / `video` / `audio` / `document` / `all` plus loose aliases (`photo`, `music`, MIME prefixes). |
| `--timeout` | `10` | Seconds per `lsext <dir>` call. Recursive walks multiply this by subdirectory count. |
| `--use-recursive` | **off** | Use `lsext-r <path>` (single recursive call) instead of the wrapper's manual DFS. **Faster on devices that support it, but hangs on OnePlus 12 and similar OPPO-derived devices.** Default is the safe option. |
| `--retry-on-hang N` | 0 | If `lsext` hangs, kill mtp-cli, wait, retry up to N times. |
| `--no-json` | off | Human-readable text instead of JSON. |

#### Why manual DFS by default

The bundled `mtp-cli` (v3.9-2, an `android-file-transfer-linux` fork)
shells out to `lsext-r` for recursive directory listings. On OnePlus 12
(and likely other OPPO-derived phones), `lsext-r` returns the first few
entries on stdout then **hangs forever** — the device's MTP state machine
stops answering GetObjectHandles for the recursive subtree. mtp-cli's hang
leaves the USB interface in a half-claimed state that breaks every
subsequent mtp-cli invocation until `sudo killall -HUP usbd` resets the
macOS USB daemon.

To avoid this, `mtp-tree` defaults to a manual DFS: one non-recursive
`lsext <path>` invocation per directory, recursing from the wrapper. Each
call returns quickly (proven to work on OnePlus 12); total wall time is
slightly higher than a single `lsext-r` would be on a working device but
predictable and bounded.

Pass `--use-recursive` to opt back into a single `lsext-r` call. The
wrapper will use the partial stdout if `lsext-r` hangs.

### `mtp-backup`

```sh
# Plan first — dry-run is the default, so this lists what WOULD be copied.
$ mtp-backup --src /DCIM/Camera --dst ~/Pictures/backup-2026-09-26 \
              --include '*.jpg' --include '*.jpeg' --format-filter image
{
  "dry_run": true,
  "src": "/DCIM/Camera",
  "dst": "/Users/you/Pictures/backup-2026-09-26",
  "include": ["*.jpg", "*.jpeg"],
  "format_filter": "image",
  "planned": [
    {"src": "/DCIM/Camera/IMG_001.jpg", "dst": ".../IMG_001.jpg",
     "size": 5242880, "category": "image", "mime": "image/jpeg"}
  ],
  "copied": [], "skipped": [], "errors": []
}

# Then actually copy. --no-dry-run is REQUIRED to write anything.
$ mtp-backup --src /DCIM/Camera --dst ~/Pictures/backup-2026-09-26 \
              --include '*.jpg' --format-filter image --no-dry-run
```

The destination layout mirrors the device path. `--src /DCIM/Camera` with
`IMG_001.jpg` writes to `<dst>/IMG_001.jpg`.

Flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--src` | (required) | Directory on the device. |
| `--dst` | (required) | Local destination. Created if missing. |
| `--include` | none | Glob to allow (repeatable). |
| `--exclude` | none | Glob to deny (repeatable, applied after include). |
| `--format-filter` | none | Same as `mtp-tree`. |
| `--depth` | unbounded | Forwarded to the internal tree listing. |
| `--dry-run` / `--no-dry-run` | `--dry-run` | **Default is safe; pass `--no-dry-run` to actually copy.** |
| `--overwrite` | off | Overwrite destination files that already exist. Default: skip. |
| `--list-timeout` | `10` | Seconds for the initial directory listing. |
| `--file-timeout` | `60` | Seconds per file transfer. Raise for large videos. |

## Output convention

All scripts follow the same shape (so wrappers don't need per-script logic):

- **stdout**: machine-readable JSON (default) or human text (`--no-json`).
- **stderr**: `Error: <message>` on failure. Logs and warnings also go here.
- **exit codes**:
  - `0` — success
  - `1` — user/input error (bad flag, no device, mtp-cli not found, no entries)
  - `2` — partial failure (`mtp-backup` only — some files copied, some errored)

## Calling from an agent

The scripts are designed to be spawned as subprocesses by an LLM agent.
Suggested workflow for the agent:

1. Run `mtp-list`. If `connected == false`, surface the error to the user.
2. Run `mtp-tree --path <the directory the user named>` to show the user
   what's there.
3. Translate the user's "back up photos from DCIM" into
   `mtp-backup --src /DCIM --dst <somewhere> --format-filter image --no-dry-run`.
4. Parse the JSON output and report counts back to the user.

See [`ishadowland/openmtp-skill`](https://github.com/ishadowland/openmtp-skill)
for the Hermes / OpenClaw packaging that wires the scripts + SKILL.md
together.

## References

- [`references/mtp-cli-commands.md`](references/mtp-cli-commands.md) — full `mtp-cli` command reference
- [`references/file-types.md`](references/file-types.md) — MTP `ObjectFormatCode` → category / MIME mapping
- [`references/troubleshooting.md`](references/troubleshooting.md) — what to do when the device isn't detected

## Status

v0.1 — three scripts + parser + tests + reference docs. Real-device
testing is pending (the maintainer's machine has no Android device at the
moment). File an issue if parsing breaks against your device's `lsext-r`
output.

## License

MIT. See [LICENSE](LICENSE).