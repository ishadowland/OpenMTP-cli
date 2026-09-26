# OpenMTP-cli

> CLI / command-line interface for OpenMTP — the Android MTP file transfer app
> for macOS, by [Ganesh Rvel](https://github.com/ganeshrvel).
>
> Status: **early scaffold / placeholder** — no code yet.
> Project homepage: https://github.com/ganeshrvel/openmtp
> Companion app (GUI): installed at `/Volumes/OpenMTP 3.3.0/OpenMTP.app`

## Why this project exists

OpenMTP ships as a macOS GUI app for fast Android MTP transfers (much faster than
macOS's built-in Photos / Image Capture flow, which scans the entire device
every connect). Some jobs are awkward in a GUI:

- Selecting 5,000 photos by hand
- Watching the queue and clicking through each "open in Finder" → eject → reconnect
- Running an incremental mirror between the phone and a NAS

A CLI can script all of that. This project will provide it.

## Planned scope (NOT IMPLEMENTED YET)

- `openmtp list` — enumerate connected Android devices + their storage volumes
- `openmtp mount <serial>` — mount the device's shared storage at `/Volumes/<name>`
- `openmtp pull <src> <dst>` — copy files / directories (resumable, parallel)
- `openmtp push <src> <dst>` — push to device
- `openmtp watch <dir>` — incremental mirror of a folder to the device
- `openmtp config` — read / write the same settings the GUI uses
  (`~/Library/Application Support/io.ganeshrvel.openmtp`)
- `openmtp eject <serial>` — cleanly disconnect a device
- JSON output (`--json`) for scripting / piping
- Library mode (`import openmtp` from Python) for embedding

## Implementation ideas (TBD)

| Option | Pro | Con |
|---|---|---|
| Wrap the bundled `OpenMTP.app` CLI / XPC service | Reuses auth + storage logic from upstream | Tight coupling to GUI's internals |
| Shell out to `adb` for MTP transfers | No code to write, works on all MTP devices | Slow for large transfers, no resume |
| Use `macOS Finder's mount_ftp` style approach via `ftpfs_agent` | Native volume mount, Finder-integrated | OpenMTP already handles MTP, no need |
| Hybrid: thin wrapper around OpenMTP.app + `adb` fallback | Best of both | More code surface |

Most likely outcome: a thin Rust or Go binary that talks to `OpenMTP.app` over its
XPC interface, with `adb` fallback for when the app isn't running.

## Status

**Scaffold only.** No code, no releases. The README above is a wishlist. PRs
welcome — start by opening an issue to discuss scope before sending code.

## Related

- [OpenMTP GUI](https://github.com/ganeshrvel/openmtp) — the macOS app this CLI wraps
- `/Volumes/OpenMTP 3.3.0/` — local install on this machine

## License

TBD. The upstream OpenMTP app is MIT-licensed, so this should follow the same
unless there's a reason not to.

---

*Placeholders and notes. Substantive content will land as decisions are made.*