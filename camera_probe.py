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
    serve      MJPEG viewer on localhost, so you can actually see the camera
    capcheck   measure the tube mouth and say whether the cap is on or off

`detect` imports nothing outside the standard library, so it runs on a bare
interpreter with no venv. `capture` and `serve` use pyrealsense2 and cv2 when
they are present and degrade honestly when they are not.

`serve` exists because the backend stack is a lot of moving parts to stand up
when the only open question is whether this machine can get a frame at all. It
is one file, one port, no build step:

    python3 camera_probe.py serve

Run it from a terminal, not from an editor or agent subprocess. On macOS the
camera grant attaches to the application responsible for the process tree, and
a terminal can be granted where many parents cannot.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import sys
import time
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
    EXCLUSIVE_ACCESS = "exclusive_access"
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
        "process. If that app is a normal one, enable it in System Settings > "
        "Privacy & Security > Camera and then fully quit and reopen it; a reload "
        "is not enough. Note that an app lacking the camera entitlement entirely "
        "can never be granted and will never appear in that list, in which case "
        "the fix is to run this from a terminal instead."
    ),
    Diagnosis.DRIVER_CLAIMED: (
        "The macOS UVC driver owns the camera interfaces, so the librealsense "
        "libusb backend cannot claim them. Use the AVFoundation path instead, or "
        "run librealsense on a Linux host where the device can be detached."
    ),
    Diagnosis.EXCLUSIVE_ACCESS: (
        "The SDK reached the device but could not power it on, because macOS "
        "UVCAssistant holds exclusive ownership of every interface. This is not "
        "a TCC permission and no setting will change it. Re-run the same command "
        "under sudo, which is the documented workaround for this SDK on macOS. "
        "If sudo also fails, move the camera to a Linux host."
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

    # Split the registry into one block per USB device node before reading any
    # property. An earlier version scanned the whole dump for the first matching
    # property and assigned it to every RealSense, which is invisibly wrong with
    # one camera attached and actively misleading with two: both were reported
    # with the same serial. A serial that is confidently wrong is worse than a
    # missing one, because it is exactly what callers match on.
    blocks: list[str] = []
    for match in re.finditer(r"\+-o .*?<class IOUSBHostDevice", raw):
        nxt = raw.find("<class IOUSBHostDevice", match.end())
        blocks.append(raw[match.start() : nxt if nxt != -1 else len(raw)])

    def block_for(dev: Device) -> str | None:
        """The registry block whose product id matches this device."""
        for block in blocks:
            pid = re.search(r'"idProduct" = (\d+)', block)
            if pid and dev.product_id is not None and int(pid.group(1)) == dev.product_id:
                return block
        return None

    for dev in devices:
        if not dev.is_realsense:
            continue
        block = block_for(dev)
        if block is None:
            continue  # leave the fields None rather than borrow another device's

        serial = re.search(r'"USB Serial Number" = "([^"]+)"', block)
        speed = re.search(r'"UsbLinkSpeed" = (\d+)', block)
        if serial:
            dev.serial = serial.group(1)
        if speed:
            dev.link_speed_bps = int(speed.group(1))
        for driver in ("UVCAssistant", "AppleUSBHostCompositeDevice"):
            if driver in block:
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


# Enumerating through librealsense is run in a throwaway subprocess rather than
# in this interpreter. On macOS the SDK cannot claim a camera that UVCAssistant
# already owns, and its failure path is not crash safe: it segfaults outright as
# often as it raises. A preflight whose entire job is to report faults cannot be
# killed by the fault it is reporting, so the crash is contained where it can be
# observed and named. The child prints one JSON line on success.
_RS_ENUMERATE = r"""
import json, sys
try:
    import pyrealsense2 as rs
except ImportError:
    print(json.dumps({"status": "missing"})); sys.exit(0)
try:
    found = [
        {
            "name": d.get_info(rs.camera_info.name),
            "serial": d.get_info(rs.camera_info.serial_number),
            "depth_scale": d.first_depth_sensor().get_depth_scale(),
        }
        for d in rs.context().query_devices()
    ]
    print(json.dumps({"status": "ok", "devices": found}))
except Exception as exc:
    print(json.dumps({"status": "error", "error": str(exc)}))
"""


def running_as_root() -> bool:
    """True when this process can seize a USB interface another driver holds.

    Root satisfies the IOKit authorisation that `USBInterfaceOpenSeize` needs, so
    it takes the interface from UVCAssistant instead of racing it. Everything
    about whether a claim is worth attempting hinges on this.
    """
    import os

    return os.geteuid() == 0


def uvc_holds_the_device(devices: list[Device]) -> bool:
    """True when the UVC stack owns the RealSense AND we cannot take it from it.

    A RealSense that librealsense has claimed DISAPPEARS from
    `system_profiler SPCameraDataType`, so its presence there means the OS holds
    the exclusive lock. Unprivileged, that lock is final: librealsense does not
    fail cleanly against it, it SIGSEGVs, and every segfault raises a "Python
    quit unexpectedly" dialog. Checking first turns a crash into a sentence.

    As root the lock is NOT final, so returning True there is wrong. An earlier
    version ignored privilege and skipped the claim unconditionally, which meant
    running under sudo -- the documented fix, which this very tool recommends --
    reported EXCLUSIVE_ACCESS without ever attempting the thing that works.
    A guard that suppresses the fix it prints is worse than no guard.
    """
    if running_as_root():
        return False
    return any(d.is_realsense for d in devices)


def probe_realsense(devices: list[Device], force: bool = False) -> ProbeResult:
    """Try the vendor SDK path, which is the only source of metric depth."""
    if not force and uvc_holds_the_device(devices):
        return ProbeResult(
            "pyrealsense2",
            Diagnosis.EXCLUSIVE_ACCESS,
            "Skipped the SDK claim without attempting it: the camera is still "
            "listed by the OS camera stack, which means UVCAssistant holds the "
            "exclusive lock and librealsense would segfault rather than fail "
            "cleanly. Re-run under sudo, which takes the lock successfully.",
        )
    try:
        done = subprocess.run(
            [sys.executable, "-c", _RS_ENUMERATE],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(
            "pyrealsense2",
            Diagnosis.EXCLUSIVE_ACCESS,
            "The SDK hung while enumerating and was killed after 30s. On macOS "
            "this is contention over a camera another process already owns.",
        )

    payload: dict[str, Any] = {}
    for line in reversed((done.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue

    if not payload:
        # No parseable line means the child died before printing. A negative
        # returncode is death by signal, which is the segfault case.
        if done.returncode != 0:
            signal_note = (
                f"killed by signal {-done.returncode}"
                if done.returncode < 0
                else f"exited {done.returncode}"
            )
            return ProbeResult(
                "pyrealsense2",
                Diagnosis.EXCLUSIVE_ACCESS,
                f"The SDK crashed while enumerating ({signal_note}). librealsense "
                "on macOS is not crash safe when it cannot claim the device. This "
                "was contained in a subprocess and did not affect this preflight.",
            )
        return ProbeResult("pyrealsense2", Diagnosis.UNKNOWN, "No output from the SDK.")

    status = payload.get("status")
    if status == "missing":
        return ProbeResult(
            "pyrealsense2",
            Diagnosis.BACKEND_MISSING,
            "pyrealsense2 is not installed in this interpreter. On macOS arm64 use "
            "`pip install pyrealsense2-macosx`, which ships a prebuilt wheel; "
            "Homebrew's librealsense provides the C++ library but no bindings.",
        )

    if status == "error":
        text = str(payload.get("error", "")).lower()
        # "failed to set power state" is what librealsense reports when it found
        # the device but UVCAssistant already owns the interfaces. It reads like
        # a hardware fault and is not one, so it gets its own diagnosis.
        if "power state" in text or "access" in text:
            return ProbeResult(
                "pyrealsense2",
                Diagnosis.EXCLUSIVE_ACCESS,
                f"{payload['error']}. The SDK found the camera but could not claim it.",
            )
        return ProbeResult("pyrealsense2", Diagnosis.UNKNOWN, str(payload.get("error")))

    found = payload.get("devices") or []
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

    # Report the depth scale. The D405 uses 1e-4 m per count where the rest of
    # the D400 series uses 1e-3, so code carried over from a D435 that assumes
    # millimetre counts reads every distance ten times too large. Never assume
    # this value; it comes from the device.
    first = found[0]
    scale = first.get("depth_scale")
    detail = f"{len(found)} device(s) visible to the SDK. {first.get('name', '')}"
    if scale:
        detail += f", depth scale {scale:.3e} m/count ({scale * 1000:.4f} mm)"
    return ProbeResult("pyrealsense2", Diagnosis.OK, detail.strip())


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

    # Print the diagnosis here rather than telling the operator to run another
    # command. Deferring the reason is how a preflight becomes the thing people
    # skip: the failure has to explain itself at the point it happens.
    print("No capture path available.\n")
    for res in (rs_result, cv_result):
        print(f"  {res.backend:<14} {res.diagnosis.value.upper()}")
        if res.detail:
            print(f"      {res.detail}")
        print(f"      fix: {REMEDY[res.diagnosis]}")
    if all(r.diagnosis is Diagnosis.BACKEND_MISSING for r in (rs_result, cv_result)):
        print(
            "\n  Both backends are missing, so this interpreter cannot capture from any\n"
            "  camera regardless of permissions. Use an interpreter that has cv2:\n"
            f"      {sys.executable} is the one you just used.\n"
            "      Try `uv run python camera_probe.py capture`, or any venv with cv2."
        )
    elif any(r.diagnosis is Diagnosis.PERMISSION_DENIED for r in (rs_result, cv_result)):
        print(f"\n  Grant camera access to: {responsible_app()}")
    return 1


def _sdk_can_claim() -> bool:
    """Whether librealsense can actually open a device, tested in a child.

    Asking in-process is not an option. The bundled librealsense 2.56.5 SIGSEGVs
    instead of erroring when it cannot claim an interface, so the question
    "can the SDK work here?" would kill whoever asked it. A child process both
    answers it and absorbs the crash: a child that dies by signal is a no.

    Cached, because starting a pipeline is not free and the answer does not
    change while a server is up.
    """
    global _SDK_CLAIM_OK
    if _SDK_CLAIM_OK is not None:
        return _SDK_CLAIM_OK

    probe = r"""
import pyrealsense2 as rs
p = rs.pipeline(); c = rs.config()
c.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
p.start(c); p.stop()
print("CLAIM_OK")
"""
    try:
        done = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=25, check=False,
        )
        _SDK_CLAIM_OK = "CLAIM_OK" in (done.stdout or "")
    except (subprocess.SubprocessError, OSError):
        _SDK_CLAIM_OK = False
    return _SDK_CLAIM_OK


_SDK_CLAIM_OK: bool | None = None


class FrameFeed:
    """One reader for the camera, shared by every viewer.

    A camera admits exactly one reader, so viewers cannot each open the device.
    The first request opens it; everyone else gets whatever the last read
    produced.

    In `synthetic` mode no device is touched at all and a generated test pattern
    is served instead. That mode exists to prove the transport works while the
    camera itself is still blocked, and every synthetic frame is stamped so it
    can never be mistaken for the bench. Do not remove that stamp.
    """

    def __init__(self, synthetic: bool = False, index: int = 0, view: str = "color",
                 serial: str | None = None, label: str = "") -> None:
        self.synthetic = synthetic
        self.index = index
        # Which physical camera this feed owns. With more than one attached,
        # leaving it None means the SDK picks for you and two feeds can fight
        # over the same device while another goes unread.
        self.serial = serial
        self.label = label
        self.view = view          # color | depth
        self.error = ""
        self.source = "none"      # which path actually produced frames
        self.depth_scale = 0.0
        self._cap: Any = None
        self._pipe: Any = None
        self._n = 0
        import threading

        self._lock = threading.Lock()

    def _start_realsense(self) -> bool:
        """Prefer the vendor SDK. It is the only path that yields metric depth.

        On macOS this also happens to be the path that works at all, because
        AVFoundation is gated on an entitlement the parent process may not have
        while librealsense talks to the device directly.
        """
        try:
            import pyrealsense2 as rs
        except ImportError:
            return False
        if uvc_holds_the_device(_cached_devices()):
            self.error = (
                "UVCAssistant holds the camera, so the SDK cannot claim it and "
                "would crash rather than fail. Re-run under sudo."
            )
            return False
        try:
            pipe = rs.pipeline()
            cfg = rs.config()
            if self.serial:
                cfg.enable_device(self.serial)
            # 640x480 rather than 720p: two D435-class cameras streaming colour
            # plus depth on one 5 Gbps bus will exhaust USB bandwidth at 720p,
            # and the failure shows up as dropped frames mid-run, not at start.
            cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
            cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
            profile = pipe.start(cfg)
        except Exception as exc:
            self.error = f"realsense: {exc}"
            return False
        # Read the scale from the device, never assume it. The D405 uses 1e-4 m
        # per count where the rest of the D400 series uses 1e-3, so a hardcoded
        # constant makes every distance ten times wrong.
        self.depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()
        self._pipe = pipe
        self.source = "pyrealsense2"
        return True

    def _read_realsense(self, view: str | None = None) -> Any | None:
        import numpy as np

        frames = self._pipe.wait_for_frames(5000)
        if (view or self.view) == "depth":
            depth = frames.get_depth_frame()
            if not depth:
                return None
            import cv2

            raw = np.asanyarray(depth.get_data())
            # Colourise for viewing only. The metric values stay in `raw`; this
            # is a picture of the measurement, not the measurement.
            return cv2.applyColorMap(
                cv2.convertScaleAbs(raw, alpha=0.03), cv2.COLORMAP_JET
            )
        color = frames.get_color_frame()
        return np.asanyarray(color.get_data()) if color else None

    def _synthetic_frame(self) -> Any:
        import cv2
        import numpy as np

        h, w = 480, 640
        self._n += 1
        frame = np.zeros((h, w, 3), dtype="uint8")
        # A moving gradient plus a sweep bar, so a frozen stream is obvious at a
        # glance. A static test card cannot tell you the pipeline stalled.
        frame[:, :, 0] = np.linspace(0, 255, w, dtype="uint8")[None, :]
        frame[:, :, 1] = np.linspace(0, 255, h, dtype="uint8")[:, None]
        x = int((self._n * 6) % w)
        cv2.rectangle(frame, (x, 0), (min(x + 8, w), h), (255, 255, 255), -1)
        cv2.putText(frame, "SYNTHETIC - NOT THE CAMERA", (18, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        cv2.putText(frame, f"frame {self._n}", (18, 100),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, "transport proof only", (18, h - 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
        return frame

    def read(self, view: str | None = None) -> Any | None:
        with self._lock:
            if self.synthetic:
                self.source = "synthetic"
                return self._synthetic_frame()

            # AVFoundation FIRST, even though it cannot give depth.
            #
            # The SDK used to be tried first, on the grounds that it is the only
            # metric-depth path. That made a live view hostage to a library that
            # is not crash safe: the bundled librealsense 2.56.5 SIGSEGVs rather
            # than erroring when it cannot claim an interface, and because the
            # attempt runs in-process it took the whole server down with it. A
            # colour frame that appears beats a depth frame that segfaults, so
            # the path that cannot crash goes first and the SDK is attempted
            # only when AVFoundation is unavailable AND a subprocess has
            # confirmed the SDK can actually claim the device.
            if self._pipe is None and self._cap is None:
                if not self._open_cv2() and _sdk_can_claim():
                    self._start_realsense()
            if self._pipe is not None:
                try:
                    frame = self._read_realsense(view)
                except Exception as exc:
                    self.error = f"realsense: {exc}"
                    return None
                if frame is not None:
                    self._n += 1
                    self.error = ""
                return frame

            if self._cap is None:
                if not self.error:
                    self.error = "no capture path opened"
                return None
            if (view or self.view) == "depth":
                # AVFoundation carries no depth. Say so on the frame rather than
                # failing the request: a broken image icon reads as a glitching
                # camera, when the truth is that this path structurally cannot
                # provide depth and the SDK path is the one to fix.
                return self._depth_unavailable_frame()
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self.error = "device opened but returned no frame"
                return None
            self.error = ""
            self._n += 1
            return frame

    def _open_cv2(self) -> bool:
        """Open this feed's camera through AVFoundation. True on success.

        Retries rather than giving up on the first refusal: OpenCV's backend
        calls requestAccessForMediaType and then spins the run loop, so opens
        issued while that request is still pending fail with
        "not authorized ... requesting" even though access is about to be
        granted. Trying once made `serve` refuse to start moments before the
        same call succeeded.
        """
        try:
            import cv2
        except ImportError:
            self.error = "cv2 is not installed in this interpreter."
            return False

        for attempt in range(6):
            cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
            if cap.isOpened():
                self._cap = cap
                self.source = "opencv"
                self.error = ""
                return True
            cap.release()
            time.sleep(0.4 if attempt < 3 else 0.8)

        self.error = (
            f"AVFoundation would not open capture index {self.index} after 6 tries"
        )
        return False

    def _depth_unavailable_frame(self) -> Any:
        import cv2
        import numpy as np

        frame = np.zeros((480, 640, 3), dtype="uint8")
        frame[:] = (32, 24, 16)
        cv2.putText(frame, "no depth on this path", (60, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
        cv2.putText(frame, "colour via AVFoundation; depth needs the", (60, 260),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
        cv2.putText(frame, "librealsense path (see preflight)", (60, 285),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
        return frame

    def jpeg(self, quality: int = 80, view: str | None = None) -> bytes | None:
        frame = self.read(view)
        if frame is None:
            return None
        import cv2

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None


# Deck console palette, matching the Vue frontend so the standalone viewer and
# the app do not look like two different products.
_PAGE = """<!doctype html><meta charset=utf-8>
<title>camera preflight</title>
<style>
 body{margin:0;background:#0b1220;color:#cad6ec;font-family:system-ui,sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:24px}
 h1{font-size:18px;margin:0 0 4px}
 .sub{font-size:12px;color:#6b7a90;margin-bottom:16px}
 .card{background:#14203a;border:1px solid #24365c;border-radius:12px;padding:16px}
 img{width:100%;border-radius:8px;background:#0b1220;display:block}
 .banner{background:#7f1d1d;color:#fff;padding:8px 12px;border-radius:8px;
         font-size:12px;font-weight:700;margin-bottom:12px;letter-spacing:.04em}
 .row{display:flex;gap:8px;align-items:center;margin-top:12px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .camrow{margin-bottom:18px}
 .camname{font-size:12px;font-weight:600;color:#cad6ec;margin-bottom:6px}
 .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#6b7a90;margin-bottom:4px}
 @media(max-width:700px){.grid{grid-template-columns:1fr}}
 button{background:#2e6bff;color:#fff;border:0;border-radius:8px;padding:8px 14px;
        font-weight:600;cursor:pointer;font-size:13px}
 pre{font-size:11px;color:#9fb0cc;white-space:pre-wrap;margin:12px 0 0}
 .err{color:#ef4444;font-size:12px}
</style>
<div class=wrap>
 <h1>camera preflight</h1>
 <div class=sub>__SUB__</div>
 <div class=card>
  __BANNER__
  __CAMS__
  <div class=row>
   <button onclick="fetch('/snapshot',{method:'POST'}).then(r=>r.json()).then(j=>
     document.getElementById('s').textContent='saved '+j.name)">Snapshot</button>
   <span id=s class=sub></span>
  </div>
  <div id=e class=err></div>
  <pre id=p>loading preflight...</pre>
 </div>
</div>
<script>
 fetch('/preflight.json').then(r=>r.json()).then(j=>{
   document.getElementById('p').textContent =
     'usable: '+j.usable+'\\n'+
     j.devices.map(d=>'  '+(d.model||d.name)+(d.serial?'  '+d.serial:'')).join('\\n')+
     '\\n'+j.backends.map(b=>'  '+b.backend+': '+b.diagnosis).join('\\n');
 });
</script>"""


_DEVICE_CACHE: list[Device] = []


def _cached_devices() -> list[Device]:
    if not _DEVICE_CACHE:
        _DEVICE_CACHE.extend(detect())
    return _DEVICE_CACHE


def serve(port: int = 8765, synthetic: bool = False, view: str = "color") -> int:
    """Serve an MJPEG view of the camera on localhost.

    Streaming is the fastest way to answer "is this thing working", and it needs
    no build step and no framework. Only localhost is bound: a camera feed is
    not something to put on a network interface by accident.
    """
    import http.server
    import socketserver

    try:
        import cv2  # noqa: F401
    except ImportError:
        print("serve needs cv2 for JPEG encoding. Use an interpreter that has it.")
        return 1

    if synthetic:
        feeds = [FrameFeed(synthetic=True, label="synthetic")]
    else:
        cams = [d for d in _cached_devices() if d.is_realsense and d.serial]
        feeds = [
            FrameFeed(serial=d.serial, index=i,
                      label=f"{d.model or d.name} ({d.serial})")
            for i, d in enumerate(cams)
        ] or [FrameFeed(label="default")]
    feed = feeds[0]
    boundary = "frameboundary"

    if not synthetic and all(f.jpeg() is None for f in feeds):
        # Refuse to start rather than serve a page whose video will never load.
        print("Cannot start: no capture path.\n")
        preflight()
        print("\nTo prove the transport works without a camera:")
        print(f"    {sys.executable} camera_probe.py serve --synthetic")
        return 1

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:
            pass  # one line per frame is not a log, it is noise

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/stream") or self.path.startswith("/depth"):
                want = "depth" if self.path.startswith("/depth") else "color"
                m = re.search(r"[?&]cam=(\d+)", self.path)
                idx = int(m.group(1)) if m else 0
                src = feeds[idx] if 0 <= idx < len(feeds) else feeds[0]
                self.send_response(200)
                self.send_header(
                    "Content-Type", f"multipart/x-mixed-replace; boundary={boundary}"
                )
                self.end_headers()
                try:
                    while True:
                        jpeg = src.jpeg(view=want)
                        if jpeg is None:
                            break  # never hold a stale frame on screen
                        self.wfile.write(
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                        )
                        self.wfile.write(jpeg + b"\r\n")
                        time.sleep(1 / 15)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if self.path.startswith("/preflight.json"):
                # Cached: detect() shells out to system_profiler and ioreg, which
                # take seconds. Re-running per request left the page showing
                # "loading" for the whole time. Enumeration does not change while
                # a stream is up, and a re-enumeration ends the stream anyway.
                devices = _cached_devices()
                if feed.source in ("pyrealsense2", "opencv"):
                    # Already streaming, so the question is answered. Re-probing
                    # here would open a second reader against the device this
                    # very server is holding, and lose.
                    results = [ProbeResult(feed.source, Diagnosis.OK,
                                           f"streaming, {feed._n} frames served")]
                else:
                    results = [probe_realsense(devices), probe_opencv(devices)]
                body = json.dumps(
                    {
                        "usable": any(r.ok for r in results),
                        "synthetic": synthetic,
                        "devices": [asdict(d) for d in devices],
                        "backends": [
                            {"backend": r.backend, "diagnosis": r.diagnosis.value,
                             "detail": r.detail}
                            for r in results
                        ],
                    }
                ).encode()
                self._send(200, "application/json", body)
                return

            sub = (
                "synthetic transport test, no device is being read"
                if synthetic
                else "live from the attached camera"
            )
            banner = (
                "<div class=banner>SYNTHETIC MODE - these frames are generated, "
                "not from the camera</div>"
                if synthetic
                else ""
            )
            cams_html = "".join(
                f"<div class=camrow><div class=camname>{f.label}</div>"
                f"<div class=grid>"
                f"<div><div class=lbl>color</div>"
                f'<img src="/stream?cam={i}" alt="color"></div>'
                f"<div><div class=lbl>depth (colourised for viewing)</div>"
                f'<img src="/depth?cam={i}" alt="depth"></div>'
                f"</div></div>"
                for i, f in enumerate(feeds)
            )
            page = (_PAGE.replace("__SUB__", sub).replace("__BANNER__", banner)
                    .replace("__CAMS__", cams_html))
            self._send(200, "text/html; charset=utf-8", page.encode())

        def do_POST(self) -> None:
            if not self.path.startswith("/snapshot"):
                self._send(404, "text/plain", b"no")
                return
            jpeg = feed.jpeg(quality=92)
            if jpeg is None:
                self._send(503, "application/json",
                           json.dumps({"error": feed.error}).encode())
                return
            import os

            os.makedirs("snapshots", exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + ("-synthetic" if synthetic else "") + ".jpg"
            with open(os.path.join("snapshots", name), "wb") as fh:
                fh.write(jpeg)
            self._send(200, "application/json",
                       json.dumps({"name": name, "bytes": len(jpeg)}).encode())

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    # Warm the enumeration cache before accepting requests, so the first page
    # load does not sit on a system_profiler call.
    _cached_devices()

    with Server(("127.0.0.1", port), Handler) as httpd:
        print(f"serving on http://127.0.0.1:{port}  {len(feeds)} camera(s)")
        for f in feeds:
            print(f"  {f.label}: source={f.source} scale={f.depth_scale or 0:.3e}")
        if feed.depth_scale:
            print(f"depth scale {feed.depth_scale:.3e} m/count")
        if synthetic:
            print("SYNTHETIC MODE: frames are generated, no device is read.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


_REF_FILE = "cap_reference.json"


def capcheck(roi: tuple[int, int, int, int], save_ref: bool = False,
             expected_mm: float = 15.0) -> int:
    """Measure the tube mouth and say whether the cap is on or off.

    Two-step by design. Record a reference with the cap ON, then check. There is
    no absolute "a cap is 15 mm" rule that survives a different tube, a different
    standoff or a moved camera, so the reference is what makes the number mean
    something, and it is cheap to retake.

        python3 camera_probe.py capcheck --roi 600,300,80,80 --save-ref  # cap on
        python3 camera_probe.py capcheck --roi 600,300,80,80             # now
    """
    import numpy as np

    from core.verification.depth_height import (  # noqa: PLC0415
        HeightStat, compare, measure_region,
    )

    try:
        import pyrealsense2 as rs
    except ImportError:
        print("capcheck needs pyrealsense2. `pip install pyrealsense2-macosx`.")
        return 1

    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, rs.format.z16, 30)
    try:
        profile = pipe.start(cfg)
    except Exception as exc:
        print(f"cannot start the camera: {exc}")
        preflight()
        return 1

    try:
        scale = profile.get_device().first_depth_sensor().get_depth_scale()
        # Discard the first frames: exposure and the depth filters have not
        # settled, and an unsettled frame is exactly the kind of plausible-but-
        # wrong reading this whole module exists to avoid.
        for _ in range(15):
            pipe.wait_for_frames(5000)
        frames = pipe.wait_for_frames(5000)
        depth = np.asanyarray(frames.get_depth_frame().get_data())
    finally:
        pipe.stop()

    print(f"depth scale {scale:.3e} m/count")
    stat = measure_region(depth, scale, roi)
    print(f"region {roi}: {stat.describe()}")

    if not stat.usable:
        print("\nNot enough valid depth in that region to measure.")
        print("Move the region onto the tube mouth, or check the standoff:")
        print("  the D405 needs at least 100 mm at 720p.")
        return 1

    if save_ref:
        with open(_REF_FILE, "w") as fh:
            json.dump({"roi": list(roi), "scale": scale, "stat": asdict(stat)}, fh)
        print(f"\nreference saved to {_REF_FILE} (cap ON)")
        print("Now take the cap off and re-run without --save-ref.")
        return 0

    try:
        with open(_REF_FILE) as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        print(f"\nNo {_REF_FILE}. Record one first with the cap ON:")
        print(f"  python3 camera_probe.py capcheck --roi {','.join(map(str, roi))} --save-ref")
        return 1

    reference = HeightStat(**saved["stat"])
    result = compare(reference, stat, expected_delta_mm=expected_mm)

    print(f"\nreference: {reference.describe()}")
    print(f"verdict:   {result.verdict.value.upper()}  confidence {result.confidence:.2f}")
    print(f"delta:     {result.delta_mm:+.1f} mm")
    print(f"           {result.detail}")
    # UNKNOWN is not a pass. Exit non-zero so a runner stops rather than
    # continuing on a measurement that did not resolve.
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        nargs="?",
        default="preflight",
        choices=["preflight", "detect", "probe", "capture", "serve", "capcheck"],
    )
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--out", default="frame", help="output prefix for capture")
    parser.add_argument("--port", type=int, default=8765, help="port for serve")
    parser.add_argument("--roi", default="", help="x,y,w,h region for capcheck")
    parser.add_argument("--save-ref", action="store_true",
                        help="record the cap-ON reference instead of comparing")
    parser.add_argument("--expected-mm", type=float, default=15.0,
                        help="expected height delta when the cap comes off")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="serve a generated test pattern instead of the camera, to prove the "
        "transport works while the device is still blocked",
    )
    args = parser.parse_args(argv)

    if args.command == "serve":
        return serve(port=args.port, synthetic=args.synthetic)

    if args.command == "capcheck":
        if not args.roi:
            print("capcheck needs --roi x,y,w,h at the tube mouth.")
            print("Use `serve` to find the pixel coordinates first.")
            return 2
        try:
            parts = tuple(int(v) for v in args.roi.split(","))
        except ValueError:
            print("--roi must be four integers, x,y,w,h")
            return 2
        if len(parts) != 4:
            print("--roi must be four integers, x,y,w,h")
            return 2
        return capcheck(parts, save_ref=args.save_ref, expected_mm=args.expected_mm)

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
