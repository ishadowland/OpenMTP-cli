# Troubleshooting

Common failures when running `mtp-list` / `mtp-tree` / `mtp-backup` against a
real Android device, and how to recover.

## 1. `Error: mtp-cli not found. ... Is OpenMTP.app mounted?`

`scripts/_openmtp.py::find_mtp_cli` could not locate the bundled `mtp-cli`
binary.

Resolution:

- Open OpenMTP.app at least once so it mounts `/Volumes/OpenMTP 3.3.0/`
  (the version number may differ in future releases).
- If the mount name is unusual, pass `--mtp-cli-path` explicitly, or set
  `OPENMTP_CLI` in the environment:

  ```sh
  export OPENMTP_CLI="/Volumes/OpenMTP 3.3.0/OpenMTP.app/Contents/Resources/bin/mtp-cli"
  ```

- More than one mount? `find_mtp_cli` picks the lexicographically smallest
  and prints a warning to stderr naming the others. Pass `--mtp-cli-path`
  to disambiguate.

## 2. `{"connected": false, "error": "mtp-cli did not respond within Xs ..."}`

`mtp-list` could not see a device within the timeout. The most common
causes, in order of likelihood:

1. **Phone is locked.** MTP on Android refuses to enumerate storage while
   the device is locked. Unlock the device, then re-run.
2. **USB mode is not set to "File transfer / MTP".** Pull down the
   notification shade after plugging in and pick "File transfer" (Pixel,
   stock Android) or "MTP" (Samsung One UI). Charging-only mode does not
   enumerate storage.
3. **No Android device is plugged in.** Sanity check with
   `system_profiler SPUSBDataType | grep -i android`.
4. **USB hub issue.** Plug directly into the Mac, not through an
   unpowered hub.
5. **Device is on a different USB mode (PTP / charging only).**

If the device is unlocked and on MTP but the script still times out,
try `--timeout 30` to rule out a slow handshake.

## 3. `Error: no entries returned for path '/X'. Check that the path exists ...`

`mtp-tree` or `mtp-backup` could not list the requested path. Causes:

- The path doesn't exist on the device. Run `mtp-tree --path /` first and
  pick a path from the JSON output.
- The path exists but the device returned no `lsext-r` rows. Some Android
  versions expose a storage at a numeric id (`0x00010001`) — try
  `mtp-tree --path /` and walk down from there.
- Permission denied. Some Android OEMs gate `Android/data/` and
  `Android/obb/` behind per-app permissions that mtp-cli can't bypass.
  The path will simply not be listed.

## 4. `mtp-cli` hangs even after a device is unplugged

`run_mtp` enforces a timeout, so this should self-heal within
`--list-timeout` / `--file-timeout` seconds and kill the child process. If
it doesn't:

- Check no orphan `mtp-cli` processes are around: `pgrep mtp-cli`.
- Kill them: `pkill -9 mtp-cli`.
- File an issue — the libusb teardown on this USB controller is buggy.

### Recovery runbook (when hang sticks across invocations)

Once mtp-cli has hung, the USB stack on macOS can be left in a half-stalled
state where **every subsequent mtp-cli invocation hangs** — even for
`device-info` — until the stack is reset. Recognise this when:

- `ioreg -p IOUSB -l | grep -A 10 OnePlus` shows `kUSBProductString = ""`
  (blank) and `IOGeneralInterest = "IOCommand is not serializable"`
- `system_profiler SPUSBDataType` still sees the phone, but
  `mtp-list` returns `connected: false` even on retry
- `IOCreatePlugInInterfaceForService: error 0xe00002be` appears on stderr
  every time

**Step-by-step recovery**, in order of intrusiveness:

1. **Wait 10 seconds.** The IOKit cache sometimes self-heals after a USB
   role-switch. While waiting, confirm the phone is **unlocked** (Android
   refuses MTP enumeration while the screen is locked).
2. **Toggle the phone's USB mode.** Settings → USB preferences (or pull
   down the notification shade after the USB plug-in notification) →
   switch from "Charging only" to "File transfer / MTP" or vice versa.
   This triggers a fresh USB role-switch that forces macOS to re-enumerate.
3. **Unplug and replug the USB cable.** Lets the phone re-enumerate its
   MTP endpoints cleanly.
4. **Reset the macOS USB daemon** (`sudo killall -HUP usbd`). This forces
   `usbd` to re-enumerate ALL USB devices; briefly disconnects other
   peripherals. Requires sudo. This is the canonical fix when steps
   1-3 don't work.
5. **Restart the Mac.** Last resort.

The wrapper also exposes `--retry-on-hang N` (default 0): on
`subprocess.TimeoutExpired` the wrapper kills mtp-cli, waits 2 seconds,
and retries up to N times before giving up. Useful for transient hangs.
The JSON output (when `connected: false`) includes `mtp_cli_attempts`,
`last_command`, and `recovery_hint` to point at the recovery steps above.

## 5. IOKit `0xe00002be` (`kIOReturnNoDevice`) in stderr but the device IS visible to OpenMTP.app

Symptom (verbatim from issue #1):

```sh
$ mtp-list
{
  "connected": false,
  "mtp_cli_stderr": "IOCreatePlugInInterfaceForService(...): error 0xe00002be\nIOCreatePlugInInterfaceForService(...): error 0xe00002be\nno mtp device found",
  ...
}
```

…and yet the GUI of OpenMTP.app just successfully copied a file from the
same phone on the same USB port a moment ago.

The `0xe00002be` is **`kIOReturnNoDevice`** from IOKit's USB device user
client. It is **not always fatal**: the bundled `mtp-cli` (v3.9-2,
android-file-transfer-linux fork) often logs the error to stderr but still
goes on to enumerate the device on stdout. Until the parser was fixed, the
wrapper saw an empty `device-info` parse and reported `connected: false`
even though the device was actually visible in `stdout`. The current
parser accepts both output formats, so the reported `connected: true` for
the same setup.

What to do if you still see `connected: false` despite the GUI working:

1. **Read `mtp_cli_stderr` first.** If the error code is `0xe00002be`, the
   USB interface claim failed. Common causes:
   - A leftover OpenMTP Helper process from a previous GUI session still
     owns the interface even though the main app quit. Try
     `pkill -9 -f "OpenMTP Helper"` and re-run.
   - macOS hasn't refreshed its IOKit device cache after a USB role
     switch (phone went from "charging" → "MTP"). Wait ~10 s, replug the
     cable, or:
     ```sh
     sudo killall -HUP usbd
     ```
     This forces `usbd` to re-enumerate. Requires sudo; briefly
     disconnects all USB devices.
2. **Try the fallback chain explicitly.** `mtp-list` already retries with
   `-C` and `-e` automatically. To force the original invocation (e.g.
   when debugging flag interactions), pass `--no-fallback`. The
   `attempted` field in the JSON output records which combinations ran.
3. **If `connected: true` was returned after the fix but the device
   fields look empty**, this is the parser-fix in action: the JSON now
   contains `manufacturer` / `model` / `device version` / `serial number`
   populated from the positional format. If those keys are still
   missing, file an issue with the `mtp-cli` output verbatim.

## 6. `Error: mtp-cli exit N` during `mtp-backup`

The file copy step failed. Look at the `errors` array in the JSON output:
each entry has `src`, `dst`, `error`, and a tail of `stderr` for diagnosis.

Common cases:

- `get` exited non-zero — usually means the device disconnected mid-copy.
  Reconnect and retry; the script skips files that already exist on
  disk, so a partial backup resumes safely.
- File vanished between `lsext-r` and `get` (e.g. a background sync deleted
  it). Re-run with `--overwrite` if you want to nuke stale local copies;
  otherwise leave it.
- Permission error on the local destination. Check `--dst` is writable.

## 7. Test on a phone you trust

MTP transfers are write/read against the user's only copy of their photos.
Before running `--no-dry-run`:

1. Run the command without `--no-dry-run` (dry-run is the default) and read
   the `planned` array.
2. Sanity-check the count and total size.
3. Run with `--no-dry-run`.

There is no "trash" on Android — `rm` on the device is recursive. None of
the scripts in this repo issue `rm`, but if you script around them with
the same `run_mtp`, **do not pipe `rm`** unless you really mean it.

## 8. Reporting bugs

Open an issue at <https://github.com/ishadowland/openmtp-cli/issues>. Include:

- Output of `mtp-list` (the JSON, even with `connected: false`).
- `system_profiler SPUSBDataType` snippet around the device.
- macOS and OpenMTP.app versions.
- The exact command line and the JSON / error you got.

Without these the maintainer can't reproduce.