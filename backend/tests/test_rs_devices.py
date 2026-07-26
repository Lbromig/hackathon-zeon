"""Enumeration must never take the caller down with it.

`test_cameras_api.py` already asserted "one or the other, never a crash" on the
/api/cameras/devices response. It could not enforce the second half: the SDK
faulted inside the request handler, and a SIGSEGV kills the interpreter before
any assertion runs, so the suite died rather than failed.

These tests cover the containment itself, so the guarantee is checked rather than
hoped for. All of them run with no camera attached.
"""
from __future__ import annotations

import subprocess

import pytest

from core.perception import rs_devices
from core.perception.rs_devices import RSEnumeration, enumerate_devices


def test_enumeration_never_raises_with_no_camera():
    """The normal bring-up state: nothing plugged in. Must answer, not raise.

    Note what is NOT asserted: that there is either a device or an error. Three
    states are all legitimate, and "no devices and no error" is the honest answer
    when nothing is attached. An earlier version of this test required one of the
    two to be non-empty and failed the moment the cameras were genuinely
    unplugged, which is the one situation it was named for. The contract is that
    the call answers rather than crashing, not that it always has something to
    report.
    """
    result = enumerate_devices()
    assert isinstance(result, RSEnumeration)
    assert isinstance(result.devices, list)
    assert isinstance(result.error, str)
    # ok means "no error", which is true both with cameras and with none.
    assert result.ok == (not result.error)


def test_a_child_killed_by_signal_becomes_an_error(monkeypatch):
    """The case that used to be fatal.

    A negative return code is death by signal. It has to surface as an error
    string, because the whole point of the subprocess is that the crash stops
    being the caller's problem.
    """
    class Killed:
        returncode = -11        # SIGSEGV
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Killed())
    result = enumerate_devices()
    assert not result.ok
    assert "signal 11" in result.error
    assert "crash" in result.error.lower()
    assert result.devices == []


def test_a_hang_is_killed_and_reported(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=25)

    monkeypatch.setattr(subprocess, "run", hang)
    result = enumerate_devices()
    assert not result.ok
    assert "hung" in result.error


def test_devices_are_parsed_including_depth_scale(monkeypatch):
    class Done:
        returncode = 0
        stderr = ""
        stdout = (
            '{"devices": [{"serial": "123", "name": "Intel RealSense D435", '
            '"firmware": "5.12", "depth_scale": 0.001}]}'
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    result = enumerate_devices()
    assert result.ok
    assert len(result.devices) == 1
    dev = result.devices[0]
    assert dev.serial == "123"
    assert dev.depth_scale == pytest.approx(1e-3)


def test_a_missing_depth_scale_stays_none_rather_than_defaulting(monkeypatch):
    """A wrong scale is worse than an absent one.

    D405 reports 1e-4 where the rest of the D400 series reports 1e-3, so
    substituting a plausible default here would silently misplace everything by
    10x. Absent must read as absent.
    """
    class Done:
        returncode = 0
        stderr = ""
        stdout = '{"devices": [{"serial": "9", "name": "x"}]}'

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    result = enumerate_devices()
    assert result.devices[0].depth_scale is None


def test_noise_before_the_json_line_is_tolerated(monkeypatch):
    """librealsense writes its own diagnostics to stdout. Take the last line."""
    class Done:
        returncode = 0
        stderr = ""
        stdout = 'ERROR (handle-libusb.h:127) failed to claim usb interface\n{"devices": []}'

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    result = enumerate_devices()
    assert result.ok
    assert result.devices == []


def test_the_probe_source_does_not_import_at_module_scope():
    """The child script must not be imported into this process.

    If the enumeration code ever ran here rather than in the child, the crash
    would be back and this test file would be the thing that dies.
    """
    assert "import pyrealsense2" in rs_devices._ENUMERATE
    assert "pyrealsense2" not in [m for m in dir(rs_devices) if not m.startswith("_")]
