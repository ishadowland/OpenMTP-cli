# `mtp-cli` command reference

The `mtp-cli` binary bundled in `OpenMTP.app` is a fork of the
[android-file-transfer-linux](https://github.com/ganeshrvel/android-file-transfer-linux)
CLI. This is what `scripts/mtp_*.py` shell out to.

In scripts we always invoke it with `-b` (batch mode), piping one or more
commands on stdin, then `quit` to end the session.

## Invocation flags

| Flag | Long | Purpose |
| --- | --- | --- |
| `-b` | `--batch` | Read commands from stdin. The default mode scripts use. |
| `-i` | `--interactive` | Force interactive (TTY) mode. Scripts do NOT use this. |
| `-f` | `--input-file <path>` | Read commands from a file (one per line). |
| `-v` | `--verbose` | Debug output to stderr. |
| `-e` | `--events` | Allow event processing. |
| `-C` | `--no-claim` | Do not exclusively claim the USB interface, so OpenMTP.app GUI and the script can run side-by-side. |
| `-R` | `--reset-device` | Reset the USB device before connecting. |
| `-V` | `--version` | Print libmtp version and exit. |

## Commands (interactive / batch mode)

The list below is taken from the binary's own `help` output. Commands are
case-sensitive.

### Device & storage

| Command | Output |
| --- | --- |
| `device-info` | `Manufacturer:`, `Model:`, `Device version:`, `Serial number:` |
| `device-properties` | Device-wide MTP properties |
| `storage-list` | One line per storage: `Storage: <id> <description> <free> free / <total> total` |
| `storage-info <id>` | Detail for a single storage |
| `format <id>` | **Destructive** — formats a storage. Do not use from scripts. |

### Navigation

| Command | Notes |
| --- | --- |
| `cd <path>` | Change directory. Paths are absolute (start with `/`) on most builds. |
| `pwd` | Print working directory. |
| `ls` | List current directory (compact). |
| `ls <path>` | List a directory. |
| `ls -r` / `ls-r` | Recursive listing (compact). |
| `lsext` | List current dir with full metadata: object id, parent, name, type, mtime, MTP ObjectFormatCode. |
| `lsext <path>` | Same, for a specific path. |
| `lsext-r` / `lsext-r <path>` | Recursive `lsext`. The workhorse command for `mtp-tree` and `mtp-backup`. |

### File operations

| Command | Notes |
| --- | --- |
| `get <file>` | Download file to current local directory. |
| `get <file> <dst>` | Download file to a specific local destination path. |
| `put <file>` | Upload file from current local directory into current MTP directory. |
| `put <file> <dir>` | Upload file into a specific MTP directory. |
| `mkdir <path>` | Create a single directory (parent must exist). |
| `mkdir-tree <path>` | Create nested directories. |
| `rm <path>` | **Recursive delete — DANGEROUS. Do not use from scripts.** |
| `rm-id <id>` | Same, by object id. |
| `stat <path>` | Type detection via libmagic; prints a type string. |
| `rename <old> <new>` | Rename within the current storage. |
| `get-thumb <file>` | Download thumbnail (for images / videos that support it). |
| `reset-device` | Reset USB. Use sparingly. |
| `quit` / `exit` | End the batch session. |

## Behavior notes (from real-device testing + binary inspection)

- **No device**: prints `no mtp device found` to stderr, exits 1 within a
  fraction of a second. Scripts detect this via `device-info` returning an
  empty parse result.
- **Device locked / not in MTP mode**: behavior depends on the OS. macOS may
  show the device as USB but the libmtp claim fails; the script's 5-second
  timeout is the safety net here.
- **`lsext-r <path>`** on a large device can take seconds. Default
  `--list-timeout` in `mtp-tree` / `mtp-backup` is 10 s. Increase via flag
  for slow devices.
- **`get` per file** spawns a fresh mtp-cli process in `mtp-backup`. Startup
  overhead is ~0.5 s; for very large backups consider batching later.

## Quoting and paths

- Filenames with spaces are passed as a single argument: `get "/DCIM/My Photos/IMG.jpg" /tmp/IMG.jpg`.
- Paths with newlines, quotes, or shell metacharacters are not supported by
  mtp-cli and should be filtered out upstream.

## See also

- `references/file-types.md` — ObjectFormatCode mapping used by `mtp-tree`.
- `references/troubleshooting.md` — what to do when the device isn't detected.