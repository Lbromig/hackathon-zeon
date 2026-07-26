#!/usr/bin/env python3
"""Depth camera preflight for the bench.

A verification rig is only as trustworthy as its sensor. Before any policy is
allowed to say "the cap came off" or "the tube is seated", something has to
establish that a camera is attached, that this process can actually open it, and
that the frames coming back are real. That is all this file does.

The design rule here is that a failure must name its own cause. "Could not open
camera" is useless at the bench. Every failure path in this file resolves to one
of a small set of diagnoses, and each diagnosis carries the specific next action.

Run with no arguments for the full preflight:

    python3 camera_probe.py

Subcommands:

    detect     enumerate attached cameras, stdlib only, never opens a stream
    probe      try every capture backend and report exactly what blocked
    capture    grab frames and write them to disk
    depth      sample metric depth over a region, for height-delta checks

`detect` imports nothing outside the standard library, so it runs on a bare
interpreter with no venv. `probe`, `capture` and `depth` use pyrealsense2 and
cv2 when they are present and degrade honestly when they are not.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

# Intel. Every RealSense shares this vendor id.
REALSENSE_VENDOR_ID = 0x8086

# Product ids for the D400 family members likely to appear on a bench. The
# depth characteristics differ enough between them that the preflight reports
# which one it found rather than just "a RealSense".
REALSENSE_PRODUCT_IDS = {
    0x0B07: "D435",
    0x0B3A: "D435i",
    0x0B5B: "D405",
    0x0B5C: "D455",
    0x0AD3: "D415",
    0x0B64: "L515",
}

# USB link speed in bits per second, as reported by ioreg's UsbLinkSpeed. The
# D400 series streams depth over USB 3. Negotiating USB 2 is a common and
# quiet failure: the device still enumerates and still opens, but high frame
# rates and larger resolutions fail later, at the worst possible moment.
USB3_MIN_BPS = 5_000_000_000


class Diagnosis(str, Enum):
    """The reason a capture path did or did not work.

    These are deliberately coarse. Each maps to exactly one operator action.
    """

    OK = "ok"
    NO_DEVICE = "no_device"
    PERMISSION_DENIED = "permission_denied"
    DRIVER_CLAIMED = "driver_claimed"
    NO_RAW_USB = "no_raw_usb"
    BACKEND_MISSING = "backend_missing"
    OPENED_BUT_NO_FRAMES = "opened_but_no_frames"
    UNKNOWN = "unknown"


# What to actually do about each diagnosis. Kept next to the enum so a new
# diagnosis cannot be added without an operator action to go with it.
REMEDY = {
    Diagnosis.OK: "Nothing. Frames are flowing.",
    Diagnosis.NO_DEVICE: (
        "No camera is attached. Plug it into a USB 3 port directly, not through "
        "a hub or a display, and use a cable rated for data rather than charging."
    ),
    Diagnosis.PERMISSION_DENIED: (
        "macOS is refusing camera access to the application responsible for this "
        "process. Open System Settings > Privacy & Security > Camera, enable the "
        "responsible app named below, then fully quit and reopen that app. A "
        "reload is not enough; the grant is read when the process tree starts."
    ),
    Diagnosis.DRIVER_CLAIMED: (
        "The macOS UVC driver owns the camera interfaces, so the librealsense "
        "libusb backend cannot claim them. Use the AVFoundation path instead, or "
        "run librealsense on a Linux host where the device can be detached."
    ),
    Diagnosis.NO_RAW_USB: (
        "This process cannot enumerate USB at all, so any libusb backend will "
        "report no device regardless of what is plugged in. Run from a terminal "
        "that has been granted the relevant access, and prefer the AVFoundation "
        "path on macOS."
    ),
    Diagnosis.BACKEND_MISSING: (
        "The backend is not installed in this interpreter. See the install notes "
        "printed alongside this diagnosis."
    ),
    Diagnosis.OPENED_BUT_NO_FRAMES: (
        "The device opened but delivered no frames. This is usually another "
        "process holding the stream, or a link that negotiated USB 2. Close other "
        "camera users and check the link speed reported by `detect`."
    ),
    Diagnosis.UNKNOWN: "Unrecognised failure. The raw error is printed above.",
}


@dataclass
class Device:
    """One attached camera, as seen without ever opening a stream."""

    name: str
    vendor_id: int | None = None
    product_id: int | None = None
    serial: str | None = None
    model: str | None = None
    link_speed_bps: int | None = None
    claimed_by: list[str] = field(default_factory=list)

    @property
    def is_realsense(self) -> bool:
        return self.vendor_id == REALSENSE_VENDOR_ID

    @property
    def is_usb3(self) -> bool | None:
        """None when the link speed could not be determined."""
        if self.link_speed_bps is None:
            return None
        return self.link_speed_bps >= USB3_MIN_BPS

    def describe(self) -> str:
        bits = [self.model or self.name]
        if self.vendor_id is not None and self.product_id is not None:
            bits.append(f"{self.vendor_id:#06x}:{self.product_id:#06x}")
        if self.serial:
            bits.append(f"serial {self.serial}")
        if self.link_speed_bps:
            gbps = self.link_speed_bps / 1e9
            usb3 = self.is_usb3
            flag = "USB3" if usb3 else "USB2, TOO SLOW FOR DEPTH"
            bits.append(f"{gbps:.0f} Gbps {flag}")
        return "  ".join(bits)


@dataclass
class ProbeResult:
    """The outcome of trying one capture backend."""

    backend: str
    diagnosis: Diagnosis
    detail: str = ""
    frames: int = 0

    @property
    def ok(self) -> bool:
        return self.diagnosis is Diagnosis.OK


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout, or an empty string on any failure.

    Every caller treats an empty result as "could not determine", so a missing
    binary and a crashed binary are handled identically and neither is fatal.
    """
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        done = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False, text=True
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return done.stdout or ""


def _system_profiler_cameras() -> list[Device]:
    """Cameras as CoreMediaIO sees them.

    This is the authoritative "is there a camera" check on macOS. It reflects
    what a capture API would be offered, which is not always what is on the USB
    bus. The plist output is parsed rather than the text output because the text
    output collapses the model id and the name into one ambiguous block.
    """
    raw = _run(["system_profiler", "-xml", "SPCameraDataType"], timeout=60)
    if not raw:
        return []
    try:
        parsed = plistlib.loads(raw.encode())
    except Exception:
        return []

    devices: list[Device] = []
    for section in parsed:
        for item in section.get("_items", []) or []:
            name = item.get("_name", "unknown")
            model = item.get("spcamera_model-id", "") or ""
            dev = Device(name=name, model=name)
            # Model ids look like: "UVC Camera VendorID_32902 ProductID_2907".
            # These are decimal, not hex, which is a genuinely easy way to
            # misidentify a device if you assume otherwise.
            vid = re.search(r"VendorID_(\d+)", model)
            pid = re.search(r"ProductID_(\d+)", model)
            if vid:
                dev.vendor_id = int(vid.group(1))
            if pid:
                dev.product_id = int(pid.group(1))
            if dev.product_id in REALSENSE_PRODUCT_IDS:
                dev.model = f"RealSense {REALSENSE_PRODUCT_IDS[dev.product_id]}"
            devices.append(dev)
    return devices


def _ioreg_usb_detail(devices: list[Device]) -> None:
    """Fill in serial, link speed and claiming drivers from the IOKit registry.

    CoreMediaIO does not expose link speed, and link speed is the difference
    between a camera that works and one that fails under load. Enriching in
    place keeps the camera list authoritative while adding the USB facts.

    Anything not found is left as None. A missing enrichment must never turn
    into a wrong claim.
    """
    raw = _run(["ioreg", "-l", "-w", "0"], timeout=60)
    if not raw:
        return

    for dev in devices:
        if not dev.is_realsense:
            continue
        # Locate the device node by product string, then read only the lines
        # belonging to it. The registry is a flat indented dump, so the node's
        # own properties are the ones before the next sibling node begins.
        start = raw.find("RealSense")
        while start != -1:
            block = raw[start : start + 20000]
            serial = re.search(r'"USB Serial Number" = "([^"]+)"', block)
            speed = re.search(r'"UsbLinkSpeed" = (\d+)', block)
            if serial and dev.serial is None:
                dev.serial = serial.group(1)
            if speed and dev.link_speed_bps is None:
                dev.link_speed_bps = int(speed.group(1))
            if dev.serial and dev.link_speed_bps:
                break
            start = raw.find("RealSense", start + 1)

        for driver in ("UVCAssistant", "AppleUSBHostCompositeDevice"):
            if driver in raw:
                dev.claimed_by.append(driver)


def detect() -> list[Device]:
    """Enumerate attached cameras without opening any of them."""
    if sys.platform != "darwin":
        return []
    devices = _system_profiler_cameras()
    _ioreg_usb_detail(devices)
    return devices


def _libusb_device_count() -> int | None:
    """How many USB devices libusb can see from this process.

    Zero is the important answer. Any libusb backend, librealsense included,
    will report "no device detected" when this is zero, and that message is
    indistinguishable from an unplugged camera unless this is checked. Returns
    None when libusb itself could not be loaded.
    """
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("usb-1.0") or "/opt/homebrew/lib/libusb-1.0.dylib"
    try:
        usb = ctypes.CDLL(path)
    except OSError:
        return None

    ctx = ctypes.c_void_p()
    if usb.libusb_init(ctypes.byref(ctx)) != 0:
        return None
    lst = ctypes.POINTER(ctypes.c_void_p)()
    count = usb.libusb_get_device_list(ctx, ctypes.byref(lst))
    usb.libusb_exit(ctx)
    return max(0, int(count))


def probe_realsense(devices: list[Device]) -> ProbeResult:
    """Try the vendor SDK path, which is the only source of metric depth."""
    try:
        import pyrealsense2 as rs  # type: ignore
    except ImportError:
        return ProbeResult(
            "pyrealsense2",
            Diagnosis.BACKEND_MISSING,
            "pyrealsense2 is not installed. `brew install librealsense` provides "
            "the C++ library and CLI tools but not the Python bindings; the "
            "bindings need a source build with -DBUILD_PYTHON_BINDINGS=ON.",
        )

    try:
        found = list(rs.context().query_devices())
    except Exception as exc:
        return ProbeResult("pyrealsense2", Diagnosis.UNKNOWN, str(exc))

    if not found:
        count = _libusb_device_count()
        if count == 0:
            return ProbeResult(
                "pyrealsense2",
                Diagnosis.NO_RAW_USB,
                "librealsense reports no device, but libusb sees zero USB devices "
                "of any kind from this process. The camera is not the problem.",
            )
        if any(d.is_realsense for d in devices):
            return ProbeResult(
                "pyrealsense2",
                Diagnosis.DRIVER_CLAIMED,
                "The camera is enumerated by the OS but invisible to librealsense, "
                "which means another driver holds its interfaces.",
            )
        return ProbeResult("pyrealsense2", Diagnosis.NO_DEVICE, "No RealSense attached.")

    return ProbeResult(
        "pyrealsense2",
        Diagnosis.OK,
        f"{len(found)} device(s) visible to the SDK.",
    )


def probe_opencv(devices: list[Device]) -> ProbeResult:
    """Try the AVFoundation path.

    This yields a 2D image only. It is worth probing anyway because it is the
    path most likely to work on macOS, and a 2D image is enough for fiducial
    localisation even when metric depth is unavailable.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        return ProbeResult(
            "opencv", Diagnosis.BACKEND_MISSING, "cv2 is not installed in this interpreter."
        )

    denied = False
    for index in range(4):
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        try:
            if not cap.isOpened():
                # OpenCV prints the authorisation failure to stderr rather than
                # raising, so an unopenable device on a machine that clearly has
                # cameras attached is the signal that the grant is missing.
                denied = True
                continue
            ok, frame = cap.read()
            if ok and frame is not None:
                return ProbeResult(
                    "opencv",
                    Diagnosis.OK,
                    f"index {index} delivered a {frame.shape[1]}x{frame.shape[0]} frame.",
                    frames=1,
                )
        finally:
            cap.release()

    if denied and devices:
        return ProbeResult(
            "opencv",
            Diagnosis.PERMISSION_DENIED,
            f"{len(devices)} camera(s) are attached but no index would open. "
            "OpenCV logs 'not authorized to capture video' in this case.",
        )
    if not devices:
        return ProbeResult("opencv", Diagnosis.NO_DEVICE, "No cameras attached.")
    return ProbeResult(
        "opencv", Diagnosis.OPENED_BUT_NO_FRAMES, "Indices opened but returned no frame."
    )


def responsible_app() -> str:
    """The application macOS attributes this process tree's permissions to.

    TCC grants attach to the responsible application, not to the interpreter, so
    naming the wrong app sends the operator to toggle a switch that changes
    nothing. Walk up the process tree and report the topmost .app bundle.
    """
    import os

    pid = os.getpid()
    topmost = "unknown"
    for _ in range(12):
        out = _run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], timeout=10).strip()
        if not out:
            break
        parts = out.split(None, 1)
        if len(parts) != 2:
            break
        parent, comm = parts[0], parts[1]
        match = re.search(r"(/[^\s]*?\.app)/Contents/MacOS/", comm)
        if match:
            topmost = match.group(1)
        if parent in ("0", "1"):
            break
        pid = int(parent)
    return topmost


def preflight(as_json: bool = False) -> int:
    """Full preflight. Returns a shell exit code: 0 only if frames flowed."""
    devices = detect()
    results = [probe_realsense(devices), probe_opencv(devices)]

    if as_json:
        payload = {
            "devices": [asdict(d) for d in devices],
            "results": [
                {"backend": r.backend, "diagnosis": r.diagnosis.value, "detail": r.detail}
                for r in results
            ],
            "responsible_app": responsible_app(),
            "usable": any(r.ok for r in results),
        }
        print(json.dumps(payload, indent=2))
        return 0 if payload["usable"] else 1

    print("Attached cameras")
    if not devices:
        print("  none")
    for dev in devices:
        marker = "*" if dev.is_realsense else " "
        print(f"  {marker} {dev.describe()}")
        if dev.is_realsense:
            if dev.is_usb3 is False:
                print("      WARNING: negotiated USB 2. Depth streaming will be unreliable.")
            if dev.claimed_by:
                print(f"      claimed by: {', '.join(sorted(set(dev.claimed_by)))}")

    print("\nCapture backends")
    for res in results:
        status = "OK" if res.ok else res.diagnosis.value.upper()
        print(f"  {res.backend:<14} {status}")
        if res.detail:
            print(f"      {res.detail}")
        if not res.ok:
            print(f"      fix: {REMEDY[res.diagnosis]}")

    usable = any(r.ok for r in results)
    print("\nVerdict")
    if usable:
        print("  A capture path is available. Depth verification can proceed.")
    else:
        print("  No capture path is available. Nothing downstream may claim to have")
        print("  observed anything.")
        if any(r.diagnosis is Diagnosis.PERMISSION_DENIED for r in results):
            print(f"  Responsible app to grant camera access to: {responsible_app()}")
    return 0 if usable else 1


def capture(out_prefix: str) -> int:
    """Write one frame from whichever backend works, for eyeballing."""
    devices = detect()
    rs_result = probe_realsense(devices)

    if rs_result.ok:
        import numpy as np  # type: ignore
        import pyrealsense2 as rs  # type: ignore

        pipeline = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.depth, rs.format.z16, 30)
        cfg.enable_stream(rs.stream.color, rs.format.bgr8, 30)
        profile = pipeline.start(cfg)
        try:
            # The first frames after start are exposure-unsettled and routinely
            # come back partly empty, so they are discarded rather than saved.
            for _ in range(10):
                pipeline.wait_for_frames()
            frames = pipeline.wait_for_frames()
            depth = frames.get_depth_frame()
            color = frames.get_color_frame()
            scale = profile.get_device().first_depth_sensor().get_depth_scale()

            import cv2  # type: ignore

            depth_m = np.asanyarray(depth.get_data()).astype("float32") * scale
            cv2.imwrite(f"{out_prefix}_color.png", np.asanyarray(color.get_data()))
            # Save depth as 16-bit millimetres. Writing a colourised preview
            # instead would discard the measurement, and the measurement is the
            # entire reason for using this camera.
            cv2.imwrite(f"{out_prefix}_depth_mm.png", (depth_m * 1000).astype("uint16"))
            print(f"wrote {out_prefix}_color.png and {out_prefix}_depth_mm.png")
            valid = depth_m[depth_m > 0]
            if valid.size:
                print(f"depth range {valid.min():.3f} m to {valid.max():.3f} m")
        finally:
            pipeline.stop()
        return 0

    cv_result = probe_opencv(devices)
    if cv_result.ok:
        import cv2  # type: ignore

        for index in range(4):
            cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
            if cap.isOpened():
                ok, frame = cap.read()
                cap.release()
                if ok and frame is not None:
                    cv2.imwrite(f"{out_prefix}_color.png", frame)
                    print(f"wrote {out_prefix}_color.png (2D only, no depth)")
                    return 0
            cap.release()

    print("No capture path available. Run `camera_probe.py` for the diagnosis.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        nargs="?",
        default="preflight",
        choices=["preflight", "detect", "probe", "capture"],
    )
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--out", default="frame", help="output prefix for capture")
    args = parser.parse_args(argv)

    if args.command == "detect":
        devices = detect()
        if args.json:
            print(json.dumps([asdict(d) for d in devices], indent=2))
        else:
            for dev in devices:
                print(dev.describe())
            if not devices:
                print("no cameras attached")
        return 0 if devices else 1

    if args.command == "capture":
        return capture(args.out)

    return preflight(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
