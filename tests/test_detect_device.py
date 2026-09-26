"""Tests for ``detect_device`` — the flag-combination fallback chain.

These tests use the ``runner=`` injection point in :func:`_openmtp.detect_device`
to return canned ``MTPResult`` values, so they do not require a real Android
device. Each test stubs the mtp-cli behaviour and asserts the chain picks the
right attempt.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _openmtp as om  # noqa: E402


def _stub_runner(scripted_results):
    """Return a callable that yields the next pre-canned MTPResult each call.

    ``scripted_results`` is a list of ``MTPResult`` values; the runner pops
    them in order. Records the ``extra_args`` it was called with so tests
    can assert which flag combinations were attempted.
    """
    queue = list(scripted_results)
    calls: list[tuple[str, ...]] = []

    def runner(commands, *, mtp_cli_path, timeout, extra_args, retries_on_hang=0):
        calls.append(tuple(extra_args))
        if not queue:
            raise AssertionError("runner called more times than scripted")
        return queue.pop(0)

    return runner, calls


class TestDetectDeviceStopsOnFirstSuccess:
    def test_returns_first_attempt_when_it_succeeds(self):
        r = om.MTPResult(
            stdout="Manufacturer: Google\nModel: Pixel\n",
            stderr="",
            timed_out=False,
            exit_code=0,
        )
        runner, calls = _stub_runner([r])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            attempts=[("default", ()), ("-C", ("-C",)), ("-e", ("-e",))],
            runner=runner,
        )
        assert detect.connected is True
        assert detect.device_info == {"manufacturer": "Google", "model": "Pixel"}
        assert detect.attempted == ["default"]
        assert calls == [()]

    def test_falls_through_to_C_when_default_fails(self):
        empty = om.MTPResult(stdout="", stderr="no mtp device found", timed_out=False, exit_code=1)
        ok = om.MTPResult(
            stdout="Manufacturer: Samsung\nModel: Galaxy\n",
            stderr="",
            timed_out=False,
            exit_code=0,
        )
        runner, calls = _stub_runner([empty, ok])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            attempts=[("default", ()), ("-C", ("-C",)), ("-e", ("-e",))],
            runner=runner,
        )
        assert detect.connected is True
        assert detect.device_info["manufacturer"] == "Samsung"
        assert detect.attempted == ["default", "-C"]
        assert calls == [(), ("-C",)]

    def test_falls_through_to_e_when_default_and_C_fail(self):
        empty1 = om.MTPResult(stdout="", stderr="", timed_out=False, exit_code=1)
        empty2 = om.MTPResult(stdout="", stderr="", timed_out=False, exit_code=1)
        ok = om.MTPResult(
            stdout="Manufacturer: Xiaomi\nModel: Mi\n",
            stderr="",
            timed_out=False,
            exit_code=0,
        )
        runner, calls = _stub_runner([empty1, empty2, ok])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            attempts=[("default", ()), ("-C", ("-C",)), ("-e", ("-e",))],
            runner=runner,
        )
        assert detect.connected is True
        assert detect.device_info["manufacturer"] == "Xiaomi"
        assert detect.attempted == ["default", "-C", "-e"]
        assert calls == [(), ("-C",), ("-e",)]


class TestDetectDeviceStopsOnTimeout:
    def test_timeout_aborts_chain_immediately(self):
        """A timeout on the first attempt means no device is on USB — retrying
        with different flags will also time out, so the chain must stop."""
        timeout_result = om.MTPResult(stdout="", stderr="", timed_out=True, exit_code=-1)
        runner, calls = _stub_runner([timeout_result])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            attempts=[("default", ()), ("-C", ("-C",)), ("-e", ("-e",))],
            runner=runner,
        )
        assert detect.connected is False
        assert detect.last_result.timed_out is True
        # Crucially: only ONE call. Do not burn the timeout budget three times.
        assert calls == [()]
        assert detect.attempted == ["default"]


class TestDetectDeviceAllAttemptsFail:
    def test_records_all_attempts_when_nothing_connects(self):
        empty = om.MTPResult(
            stdout="",
            stderr="IOCreatePlugInInterfaceForService(...): error 0xe00002be\nno mtp device found",
            timed_out=False,
            exit_code=1,
        )
        runner, calls = _stub_runner([empty, empty, empty])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            attempts=[("default", ()), ("-C", ("-C",)), ("-e", ("-e",))],
            runner=runner,
        )
        assert detect.connected is False
        # All three attempts ran and all were recorded.
        assert detect.attempted == ["default", "-C", "-e"]
        assert calls == [(), ("-C",), ("-e",)]
        # The stderr from the LAST attempt is preserved — that's the most
        # recent signal and usually the most informative for diagnosis.
        assert "0xe00002be" in detect.last_result.stderr

    def test_storage_only_count_as_connected(self):
        """A device may answer ``storage-list`` but not ``device-info`` on
        some libmtp builds; that still counts as connected."""
        r = om.MTPResult(
            stdout=(
                "Storage: 0x00010001  Phone storage  100 GB free / 256 GB total\n"
            ),
            stderr="",
            timed_out=False,
            exit_code=0,
        )
        runner, _ = _stub_runner([r])
        detect = om.detect_device(
            mtp_cli_path=Path("/fake/mtp-cli"),
            runner=runner,
        )
        assert detect.connected is True
        assert detect.device_info == {}
        assert detect.storages and detect.storages[0]["id"] == "0x00010001"


class TestDetectDeviceDefaultChainShape:
    def test_default_attempts_constant_is_well_formed(self):
        """The default ``MTP_DETECT_ATTEMPTS`` chain must be non-empty and
        each entry must be ``(label, flags_tuple)``. Tests + production code
        both rely on this shape."""
        assert len(om.MTP_DETECT_ATTEMPTS) >= 1
        for entry in om.MTP_DETECT_ATTEMPTS:
            assert isinstance(entry, tuple) and len(entry) == 2
            label, flags = entry
            assert isinstance(label, str)
            assert isinstance(flags, tuple)
            assert all(isinstance(f, str) for f in flags)

    def test_default_chain_does_not_include_destructive_flags(self):
        """``-R`` resets the USB device (destructive on some phones);
        ``-v`` is verbose (just noise in default path). Neither should be
        in the automatic fallback chain — gate them behind explicit user
        opt-in instead."""
        all_flags = [f for _, flags in om.MTP_DETECT_ATTEMPTS for f in flags]
        assert "-R" not in all_flags, "-R (reset device) is destructive; don't auto-retry"
        assert "-v" not in all_flags, "-v (verbose) shouldn't be in the default chain"


class TestMTPResultShape:
    """The MTPResult dataclass gained `attempts` and `last_command` so the
    wrapper can tell the caller *which* command hung and *how many* retries
    were spent. Lock that shape down."""

    def test_attempts_defaults_to_one(self):
        r = om.MTPResult(stdout="", stderr="", timed_out=False, exit_code=0)
        assert r.attempts == 1
        assert r.last_command == ""

    def test_last_command_round_trips(self):
        r = om.MTPResult(
            stdout="", stderr="", timed_out=True, exit_code=-1,
            attempts=3, last_command="lsext-r /DCIM",
        )
        assert r.attempts == 3
        assert r.last_command == "lsext-r /DCIM"


class TestRunMtpRetriesOnHang:
    """``run_mtp(retries_on_hang=N)`` must retry when mtp-cli hangs, and
    surface the total attempt count + last command in the result."""

    @staticmethod
    def _make_hang_proc(call_counter):
        """Return a FakeProc subclass that hangs when ``timeout`` is given
        (simulating a real mtp-cli hang) but returns empty data when called
        without a timeout (simulating the post-``kill()`` reaping that
        happens after the wrapper times out).
        """
        import subprocess as _sp

        class FakeProc:
            def __init__(self, *a, **kw):
                call_counter["n"] += 1

            def communicate(self, input=None, timeout=None):
                if timeout is not None:
                    raise _sp.TimeoutExpired("mtp-cli", timeout)
                return ("", "")

            def kill(self):
                pass

        return FakeProc

    def test_no_retry_by_default(self, monkeypatch):
        counter = {"n": 0}
        monkeypatch.setattr(om.subprocess, "Popen", self._make_hang_proc(counter))
        r = om.run_mtp(["lsext-r /DCIM"], mtp_cli_path=Path("/fake/mtp-cli"), timeout=1)
        assert r.timed_out is True
        assert r.attempts == 1
        assert counter["n"] == 1

    def test_retry_eventually_succeeds(self, monkeypatch):
        # Hang the first two invocations; return data on the third.
        # The post-kill communicate() (called by run_mtp with no timeout
        # after TimeoutExpired) must return empty data, NOT re-raise.
        import subprocess as _sp

        state = {"n": 0}

        class FakeProc:
            returncode = 0

            def __init__(self, *a, **kw):
                state["n"] += 1
                self.n = state["n"]

            def communicate(self, input=None, timeout=None):
                if timeout is not None and self.n < 3:
                    # First / second call: simulate hang
                    raise _sp.TimeoutExpired("mtp-cli", timeout)
                # Third call (success) OR post-kill reaping: return data
                if self.n >= 3:
                    return ("data\n", "")
                return ("", "")

            def kill(self):
                pass

        monkeypatch.setattr(om.subprocess, "Popen", FakeProc)
        monkeypatch.setattr(om.time, "sleep", lambda _: None)

        r = om.run_mtp(
            ["lsext-r /DCIM"],
            mtp_cli_path=Path("/fake/mtp-cli"),
            timeout=1,
            retries_on_hang=2,
        )
        assert r.timed_out is False
        assert r.attempts == 3  # initial + 2 retries
        assert r.stdout == "data\n"

    def test_retry_exhausts_and_returns_timed_out(self, monkeypatch):
        counter = {"n": 0}
        monkeypatch.setattr(om.subprocess, "Popen", self._make_hang_proc(counter))
        monkeypatch.setattr(om.time, "sleep", lambda _: None)

        r = om.run_mtp(
            ["lsext-r /DCIM"],
            mtp_cli_path=Path("/fake/mtp-cli"),
            timeout=1,
            retries_on_hang=3,
        )
        assert r.timed_out is True
        assert r.attempts == 4  # initial + 3 retries, all failed
        assert r.last_command == "lsext-r /DCIM"

    def test_last_command_is_first_command(self, monkeypatch):
        counter = {"n": 0}
        monkeypatch.setattr(om.subprocess, "Popen", self._make_hang_proc(counter))
        r = om.run_mtp(
            ["device-info", "select-storage 65537", "lsext-r /DCIM"],
            mtp_cli_path=Path("/fake/mtp-cli"),
        )


class TestHangRecoveryHint:
    def test_hint_mentions_recovery_steps(self):
        r = om.MTPResult(
            stdout="", stderr="IOCreatePlugInInterfaceForService: error 0xe00002be",
            timed_out=True, exit_code=-1,
            attempts=3, last_command="lsext-r /DCIM",
        )
        hint = om.hang_recovery_hint(r)
        assert "sudo killall -HUP usbd" in hint
        assert "Unplug and replug the USB cable" in hint
        assert "lsext-r /DCIM" in hint  # echoes the offending command
        assert "--retry-on-hang" in hint