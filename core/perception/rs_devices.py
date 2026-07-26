"""Enumerate RealSense units without risking the calling process.

`rs.context().query_devices()` looks like an ordinary call that either returns or
raises, and on Linux it is. On macOS it is not: when librealsense cannot claim a
device's USB interfaces, because UVCAssistant already holds them, the bundled
2.56.5 build faults instead of returning an error. A SIGSEGV is not an exception,
so no `try`/`except` around the call can catch it, and the process dies.

That is fatal in a server. An endpoint that enumerates cameras would take the
whole backend down with it, and the frontend asking "which cameras are attached?"
is enough to trigger it.

So the call happens in a child process. The child either prints one JSON line or
dies, and a child that dies by signal is simply an error result. The cost is a
process spawn per enumeration, which is irrelevant next to the USB round trips
the call already makes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field

# Runs in the child. Kept as a string rather than an importable function so the
# child needs nothing on its path beyond pyrealsense2 itself.
_ENUMERATE = r"""
import json, sys
try:
    import pyrealsense2 as rs
except Exception as exc:
    print(json.dumps({"error": "pyrealsense2 unavailable: %s" % exc})); sys.exit(0)
try:
    out = []
    for d in rs.context().query_devices():
        entry = {
            "serial": d.get_info(rs.camera_info.serial_number),
            "name": d.get_info(rs.camera_info.name),
            "firmware": d.get_info(rs.camera_info.firmware_version),
        }
        try:
            entry["depth_scale"] = d.first_depth_sensor().get_depth_scale()
        except Exception:
            entry["depth_scale"] = None
        out.append(entry)
    print(json.dumps({"devices": out}))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
"""


@dataclass
class RSDevice:
    serial: str
    name: str
    firmware: str = ""
    # Metres per depth count, read from the device. Never assume it: a D405
    # reports 1e-4 where the rest of the D400 series reports 1e-3, so a
    # hardcoded constant is wrong by 10x in a way that still looks plausible.
    depth_scale: float | None = None


@dataclass
class RSEnumeration:
    devices: list[RSDevice] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def enumerate_devices(timeout: float = 25.0) -> RSEnumeration:
    """Attached RealSense units. Never raises, never crashes the caller."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", _ENUMERATE],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return RSEnumeration(
            error=f"SDK enumeration hung and was killed after {timeout:.0f}s, which "
            "on macOS means contention over a camera another process owns"
        )
    except OSError as exc:
        return RSEnumeration(error=f"could not start the SDK probe: {exc}")

    payload: dict = {}
    for line in reversed((done.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue

    if not payload:
        # Nothing parseable means the child died before printing. A negative
        # return code is death by signal, which is the fault this module exists
        # to contain.
        if done.returncode < 0:
            return RSEnumeration(
                error=f"the SDK crashed while enumerating (signal {-done.returncode}). "
                "librealsense on macOS is not crash safe when it cannot claim a "
                "device; this was contained in a subprocess"
            )
        detail = (done.stderr or "").strip().splitlines()
        return RSEnumeration(
            error=f"SDK probe exited {done.returncode} with no result"
            + (f": {detail[-1]}" if detail else "")
        )

    if payload.get("error"):
        return RSEnumeration(error=str(payload["error"]))

    return RSEnumeration(devices=[
        RSDevice(
            serial=str(d.get("serial", "")),
            name=str(d.get("name", "")),
            firmware=str(d.get("firmware", "")),
            depth_scale=d.get("depth_scale"),
        )
        for d in payload.get("devices", [])
    ])
