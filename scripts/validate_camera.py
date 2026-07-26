#!/usr/bin/env python3
"""Validate that a RealSense camera actually streams, through the real driver stack.

Walks the chain one link at a time — SDK import, device enumeration, USB claim, stream
start, frame delivery, intrinsics, metric depth — and stops at the first broken link with
a message that names the fix. Each stage failing means something specific, so a bare
"no device" is never the whole answer.

Like scripts/validate_motion.py this drives the real driver (RealSenseCameraDriver ->
CameraDriver -> pyrealsense2), not raw SDK calls, so it doubles as an integration check.

macOS notes (this is where it usually stops)
--------------------------------------------
* There is **no `pyrealsense2` wheel for macOS** on PyPI — Linux and Windows only. The
  module has to be built from librealsense source with `-DBUILD_PYTHON_BINDINGS=ON`.
* On macOS 12+ the SDK needs **root**. It reaches the camera through libusb, and claiming
  a UVC interface means seizing it from macOS's own UVC driver, which only root may do
  (upstream `doc/installation_osx.md`). Without it `libusb_claim_interface` returns
  `RS2_USB_STATUS_ACCESS` and the SDK surfaces "failed to set power state" — which reads
  like a missing camera but is a privilege problem. So: run this script under `sudo`.
* The macOS **Camera** privacy permission is a *separate* gate that gates AVFoundation.
  Having it granted does not remove the root requirement, so don't let a green
  System-Settings checkbox convince you the permission side is the issue.

Usage (run from the repo root; on macOS prefix with sudo per the note above — pass the venv
interpreter explicitly, since sudo does not inherit an activated venv):
    sudo .venv/bin/python scripts/validate_camera.py            # probe + one frameset
    sudo .venv/bin/python scripts/validate_camera.py --serial 125123020017
    sudo .venv/bin/python scripts/validate_camera.py --frames 30    # sustained stream
    sudo .venv/bin/python scripts/validate_camera.py --save /tmp/shot   # colour+depth PNGs
    sudo .venv/bin/python scripts/validate_camera.py --fiducials       # tag detector

Multi-camera rig (up to three; `core/config.py` defines the three fleet slots):
    sudo .venv/bin/python scripts/validate_camera.py --list   # SDK serials + .env block
    sudo .venv/bin/python scripts/validate_camera.py --all --frames 30   # all at once

`--list` is the one to start from. The serial the SDK matches on differs from the one
ioreg/system_profiler print, and `cfg.enable_device()` compares against the SDK value — so
a camera pinned with a USB-descriptor serial binds nothing, silently.

`--all` opens every attached camera simultaneously, which is how the backend runs them
(camera_hub holds one worker per camera). Cameras that each pass alone can still fail
together once the USB budget or the host controller's endpoints run out.

On Linux no sudo is needed once the SDK's udev rules are installed:
    uv run python scripts/validate_camera.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, ".")

# Resolution/fps defaults match the driver's, which match what the D435 advertises for
# both colour and depth (848x480 is the depth sensor's native pick if 720p is unstable).
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30

OK = "  ok  "
FAIL = " FAIL "
WARN = " warn "


def _line(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def stage_import() -> object | None:
    """pyrealsense2 present? On macOS this is the most common stopping point."""
    try:
        import pyrealsense2 as rs
    except Exception as exc:
        _line(FAIL, f"cannot import pyrealsense2: {exc}")
        print(
            "\n       No macOS wheel exists on PyPI (manylinux + win_amd64 only), so pip\n"
            "       cannot supply this. Build it from source against the same interpreter:\n\n"
            "         brew install librealsense          # C++ lib + rs-* CLI tools\n"
            "         curl -sL -o lrs.tar.gz \\\n"
            "           https://github.com/realsenseai/librealsense/archive/refs/tags/v2.58.3.tar.gz\n"
            "         tar xzf lrs.tar.gz && cd librealsense-2.58.3\n"
            "         cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \\\n"
            "           -DBUILD_PYTHON_BINDINGS=ON -DPYTHON_EXECUTABLE=$(which python) \\\n"
            "           -DBUILD_EXAMPLES=OFF -DBUILD_GRAPHICAL_EXAMPLES=OFF\n"
            "         cmake --build build --target pyrealsense2 --parallel\n\n"
            "       Then put build/wrappers/python/ on PYTHONPATH (or copy the .so into\n"
            "       site-packages). The SDK and the bindings must share an architecture —\n"
            "       an x86_64 python cannot load an arm64 module or vice versa."
        )
        return None

    ver = getattr(rs, "__version__", None) or getattr(rs, "__full_version__", "unknown")
    _line(OK, f"pyrealsense2 {ver}")
    _line("      ", f"loaded from {getattr(rs, '__file__', '?')}")
    return rs


def stage_enumerate(rs) -> list:
    """Devices the SDK can see. The device *list* can be non-empty while reading any
    device's info still fails, because the count comes from USB descriptors but the info
    read needs the interface claimed — so this is where a permission denial usually lands.
    A device listed here is not yet proof that streaming will work."""
    try:
        devices = list(rs.context().query_devices())
    except Exception as exc:
        _line(FAIL, f"could not query devices: {exc}")
        _explain_claim_failure(exc)
        return []

    if not devices:
        _line(FAIL, "no RealSense devices enumerated")
        print(
            "       Check the cable is in a USB3 port and is a data cable, then confirm\n"
            "       the OS sees it at all:  ioreg -p IOUSB -w0 -l | grep -i realsense"
        )
        return []

    _line(OK, f"{len(devices)} device(s) enumerated")
    for d in devices:
        def _get(key: str) -> str:
            try:
                return d.get_info(getattr(rs.camera_info, key))
            except Exception:
                return "?"

        _line("      ", f"{_get('name')}  serial={_get('serial_number')}  "
                        f"fw={_get('firmware_version')}  usb={_get('usb_type_descriptor')}")
    return devices


def _camera_authorization() -> str | None:
    """Ask AVFoundation (via OpenCV, in a subprocess so its native stderr is capturable)
    whether this process tree may use a camera at all.

    This is the cross-check that separates a permission denial from a hardware fault: it
    goes through a completely different API than librealsense, so if it *also* reports
    unauthorized, the RealSense is not the problem. Returns a verdict string, or None if
    the check itself could not run.
    """
    import subprocess

    code = "import cv2; c = cv2.VideoCapture(0); print('OPENED', c.isOpened()); c.release()"
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=30)
    except Exception:
        return None

    blob = (p.stderr + p.stdout).lower()
    if "not authorized" in blob:
        # status 0 == AVAuthorizationStatusNotDetermined (never prompted);
        # status 2 == Denied. Either way no camera can be opened.
        status = "never prompted" if "status 0" in blob else "denied"
        return f"DENIED by macOS ({status})"
    if "opened true" in blob:
        return "granted (AVFoundation could open a camera)"
    return None


def _explain_claim_failure(exc: Exception) -> None:
    """Turn the SDK's misleading errors into the actual cause.

    "No device connected" is included deliberately: unprivileged, the SDK cannot claim the
    camera and so reports it as absent, which is indistinguishable from an unplugged cable.
    The two are separated by asking the OS — ioreg needs no privileges and no claim, so a
    unit visible there but invisible to the SDK is a permission problem, not a missing
    camera. Claiming "you need root" when nothing is plugged in would be its own wrong turn.
    """
    text = str(exc).lower()
    permission_shaped = any(
        s in text for s in ("power state", "access", "permission", "claim", "busy",
                            "no device connected")
    )
    if not permission_shaped:
        return

    topo = _usb_root_hubs()
    if topo is None or topo[0] == 0:
        _line(FAIL, "the OS does not see a RealSense on USB either — this one really is "
                    "unplugged")
        print(
            "       Use a USB3 port directly (not a hub or a monitor) and a data-rated\n"
            "       cable, then confirm:  ioreg -p IOUSB -w0 -l | grep -i realsense"
        )
        return
    _line("      ", f"the OS sees {topo[0]} RealSense unit(s) on USB, but the SDK cannot "
                    "claim them — so the hardware is fine")

    if os.geteuid() != 0:
        print(
            "\n       On macOS 12+ this means the process needs **root**. librealsense talks to\n"
            "       the camera through libusb, and claiming a UVC interface requires seizing it\n"
            "       from macOS's own UVC driver — which only root may do. Upstream states it\n"
            "       plainly in doc/installation_osx.md:\n\n"
            "         \"sudo required for USB access. On macOS 12+ most librealsense tools that\n"
            "          use libusb must be run with elevated privileges. This is due to macOS USB\n"
            "          security changes and overriding the default UVC driver.\"\n\n"
            "       Re-run this script as root (same interpreter, so the venv still applies):\n"
            f"         sudo {sys.executable} {' '.join(sys.argv)}\n\n"
            "       Cross-check outside python:  sudo rs-enumerate-devices -s\n"
        )
        verdict = _camera_authorization()
        if verdict:
            # Reported second and only as context: a granted TCC camera permission does NOT
            # remove the root requirement, so this line must not read as the fix.
            _line("      ", f"(for reference, macOS camera privacy is {verdict} — "
                            "relevant for AVFoundation, but root is still required here)")
        print(
            "       Note: needing root is a real constraint for the backend, which should not\n"
            "       run as root. Options: run only the camera capture in a small root helper\n"
            "       process and hand frames over IPC, or put the cameras on a Linux host where\n"
            "       a udev rule grants plain-user access (the SDK's intended deployment)."
        )
        return

    # Already root and still refused — the interface is genuinely held by something else.
    print(
        "\n       Running as root and the claim still failed, so another process holds the\n"
        "       device. Close anything using the camera (realsense-viewer, Photo Booth, a\n"
        "       browser tab, another copy of this script), or replug it, and retry."
    )


def stage_stream(serial: str | None, width: int, height: int, fps: int,
                 frames: int, device_id: str = "validate_cam"
                 ) -> tuple[object | None, dict | None]:
    """Start the real driver and pull framesets. Returns (driver, last_rgbd)."""
    from drivers.base import DriverError
    from drivers.camera.realsense import RealSenseCameraDriver

    cfg = {"serial": serial or "", "width": width, "height": height, "fps": fps}
    drv = RealSenseCameraDriver(device_id, cfg)

    try:
        drv.connect()
    except DriverError as exc:
        _line(FAIL, f"driver connect failed: {exc}")
        _explain_claim_failure(exc)
        return None, None
    except Exception as exc:
        _line(FAIL, f"pipeline start failed: {exc}")
        _explain_claim_failure(exc)
        print(
            f"\n       If the message mentions an unsupported configuration, this "
            f"{width}x{height}@{fps}\n"
            "       combination may not be advertised for both streams. Try --width 848 "
            "--height 480."
        )
        return None, None

    _line(OK, f"streaming {width}x{height} @ {fps} (colour bgr8 + depth z16, aligned)")

    intr = drv.intrinsics()
    if intr:
        _line(OK, f"intrinsics fx={intr['fx']:.1f} fy={intr['fy']:.1f} "
                  f"cx={intr['cx']:.1f} cy={intr['cy']:.1f}")
    else:
        _line(WARN, "no intrinsics reported")

    # First frames after start are often dropped while auto-exposure settles, so the
    # count below is a delivery check, not a latency benchmark.
    last: dict | None = None
    t0 = time.time()
    delivered = 0
    for _ in range(max(1, frames)):
        try:
            color, depth = drv.capture_rgbd()
        except Exception as exc:
            _line(FAIL, f"frame {delivered + 1} failed: {exc}")
            break
        delivered += 1
        last = {"color": color, "depth": depth}

    if delivered == 0:
        _line(FAIL, "no framesets delivered")
        return drv, None

    dt = time.time() - t0
    rate = f"{delivered / dt:.1f} fps" if dt > 0 else "n/a"
    _line(OK, f"{delivered}/{max(1, frames)} framesets delivered ({rate})")

    color, depth = last["color"], last["depth"]
    _line(OK, f"colour {color.shape} {color.dtype}   depth {depth.shape} {depth.dtype} (metres)")

    # A depth map of all zeros means the projector/stereo pair produced nothing — the
    # stream is alive but the data is useless, which a shape check alone would pass.
    h, w = depth.shape[:2]
    center = float(depth[h // 2, w // 2])
    valid = float((depth > 0).mean() * 100.0)
    if valid < 1.0:
        _line(FAIL, f"depth is essentially empty ({valid:.2f}% valid) — check the "
                    "scene is 0.2-10 m away and not a mirror/dark surface")
    else:
        tag = OK if valid > 20.0 else WARN
        _line(tag, f"depth valid {valid:.1f}% of pixels, centre pixel = "
                   f"{center:.3f} m" + ("  (0 = no return at centre)" if center == 0 else ""))
    return drv, last


# --- multi-camera (the three-camera rig) -------------------------------------------
# Wire cost per camera per second. Colour leaves the D435 as YUYV (2 bytes/px) and is
# converted to BGR on the host, so the USB cost is 2 — not the 3 that the numpy frame
# suggests. Depth is Z16, also 2. Getting this wrong understates the bus load by a third.
WIRE_BYTES_PER_PX = 2 + 2
# Practical SuperSpeed (USB 3.0, 5 Gbps) payload ceiling per host controller. The raw
# figure is 5 Gbps, but framing/protocol overhead puts real throughput near 400 MB/s, and
# cameras sharing a root hub share this budget.
USB3_PRACTICAL_BYTES_S = 400e6


def _bandwidth_note(count: int, width: int, height: int, fps: int) -> None:
    """Report the aggregate wire cost. Over-subscribing USB is the characteristic
    multi-camera failure: each camera opens fine alone, then frames stop once the
    second or third starts, which reads as a flaky camera rather than a full bus."""
    per = width * height * WIRE_BYTES_PER_PX * fps
    total = per * count
    _line("      ", f"wire cost ~{per/1e6:.0f} MB/s per camera, "
                    f"~{total/1e6:.0f} MB/s for {count} "
                    f"({total*8/1e9:.2f} Gbps of a ~{USB3_PRACTICAL_BYTES_S*8/1e9:.1f} Gbps "
                    "practical budget per controller)")
    if total > USB3_PRACTICAL_BYTES_S:
        _line(WARN, f"{width}x{height}@{fps} x{count} over-subscribes one USB3 controller — "
                    "spread the cameras across separate ports/controllers, or drop to "
                    "--width 848 --height 480 (or --fps 15)")


def _usb_root_hubs() -> tuple[int, int] | None:
    """(RealSense units seen on USB, distinct root hubs they sit on), or None.

    Read straight from ioreg rather than the SDK because it needs no privileges and no
    device claim. Cameras on the same root hub share one bandwidth budget, so this is what
    decides whether the estimate above is per-camera-fine but collectively fatal.

    Deliberately not matched up to SDK serials: the USB descriptor exposes a *different*
    serial than the SDK reports, so pairing the two would invent a mapping. The count is
    all that is needed here.
    """
    import re
    import subprocess

    try:
        raw = subprocess.run(["ioreg", "-p", "IOUSB", "-w0", "-l"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None

    hubs: set[int] = set()
    units = 0
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if "RealSense" not in line or "USB Product Name" not in line:
            continue
        units += 1
        # locationID is a sibling property of the same node, so take the next one below.
        for probe in lines[i:i + 40]:
            m = re.search(r'"locationID" = (\d+)', probe)
            if m:
                hubs.add(int(m.group(1)) >> 24)   # top byte identifies the root hub
                break
    return (units, len(hubs)) if units else None


def stage_list(rs, devices: list) -> int:
    """Print each unit's SDK serial and a ready-to-paste .env block.

    This exists because the serial the SDK matches on is *not* the one ioreg,
    system_profiler, or any USB-level tool prints. `cfg.enable_device()` compares against
    the SDK value, so pinning a camera with a USB-descriptor serial silently binds nothing.
    Reading them from here removes the guess.

    The alternative discovery route, GET /api/cameras/devices, needs the backend running —
    which on macOS would mean running FastAPI as root. This does not.
    """
    from core.config import CAM_ENV, DEFAULT_FLEET

    serials: list[str] = []
    print()
    for d in devices:
        def _get(key: str) -> str:
            try:
                return d.get_info(getattr(rs.camera_info, key))
            except Exception:
                return "?"
        serial = _get("serial_number")
        serials.append(serial)
        _line(OK, f"{_get('name'):<42} serial={serial}  fw={_get('firmware_version')}")

    # Fleet order defines which env var owns which viewpoint; read it from the config
    # rather than restating it, so a fleet edit cannot silently desync this output.
    slots = [(e["id"], CAM_ENV[e["id"]]) for e in DEFAULT_FLEET
             if e.get("type") == "realsense" and e.get("id") in CAM_ENV]

    print(f"\n.env block ({len(serials)} attached, {len(slots)} slots) — assignment below is\n"
          "positional, so reorder it to match which camera is physically where:\n")
    # Pad to a common width so the comments line up in the pasted block.
    pad = max((len(v) + len(serials[i]) if i < len(serials) else len(v))
              for i, (_, v) in enumerate(slots)) + 1
    for i, (fleet_id, env_var) in enumerate(slots):
        assignment = f"{env_var}={serials[i] if i < len(serials) else ''}"
        suffix = "" if i < len(serials) else " — no camera attached for this slot"
        print(f"{assignment:<{pad}}  # {fleet_id}{suffix}")

    if len(serials) > len(slots):
        _line(WARN, f"{len(serials)} cameras attached but only {len(slots)} fleet slots — "
                    f"add another 'realsense' entry to DEFAULT_FLEET and CAM_ENV in "
                    f"core/config.py to use the extra unit")
    print("\nLeaving a serial blank binds by enumeration order, which is stable only with a\n"
          "single camera — with two or more, an unpinned rig silently swaps viewpoints\n"
          "between runs and every pose it reports is attributed to the wrong camera.")
    return 0


def stage_concurrent(serials: list[str], width: int, height: int, fps: int,
                     frames: int) -> int:
    """Open every camera at once and pull from all of them.

    Cameras that each pass individually can still fail together, so validating them one at
    a time proves nothing about the rig. This opens them simultaneously — the condition the
    backend actually runs in, since camera_hub holds one worker per camera concurrently.
    """
    from drivers.base import DriverError
    from drivers.camera.realsense import RealSenseCameraDriver

    _bandwidth_note(len(serials), width, height, fps)
    topo = _usb_root_hubs()
    if topo:
        units, hubs = topo
        tag = OK if hubs >= units else WARN
        _line(tag, f"{units} RealSense unit(s) across {hubs} USB root hub(s)"
                   + ("" if hubs >= units else " — they share a bandwidth budget"))

    drivers = []                      # [(serial, driver)] in the order they opened
    try:
        for i, serial in enumerate(serials):
            cfg = {"serial": serial, "width": width, "height": height, "fps": fps}
            drv = RealSenseCameraDriver(f"cam{i}", cfg)
            try:
                drv.connect()
            except (DriverError, Exception) as exc:
                _line(FAIL, f"camera {i + 1}/{len(serials)} (serial {serial}) failed to "
                            f"start while {len(drivers)} other(s) were streaming: {exc}")
                if drivers:
                    print(
                        "       Earlier cameras opened, so this is contention rather than a\n"
                        "       broken unit: either the bus budget above is exhausted or the\n"
                        "       controller is out of endpoints. Lower the resolution/fps, or\n"
                        "       move this camera to a port on another controller."
                    )
                _explain_claim_failure(exc)
                return 5
            drivers.append((serial, drv))
            _line(OK, f"camera {i + 1}/{len(serials)} serial {serial} streaming")

        # Round-robin so every camera is pulled in the same window: draining one fully
        # first would leave the others' buffers to overflow and misreport them as dropping.
        delivered = {s: 0 for s, _ in drivers}
        valid_pct = {s: 0.0 for s, _ in drivers}
        t0 = time.time()
        for _ in range(max(1, frames)):
            for serial, drv in drivers:
                try:
                    _, depth = drv.capture_rgbd()
                except Exception as exc:
                    _line(FAIL, f"serial {serial} stopped after "
                                f"{delivered[serial]} frameset(s): {exc}")
                    return 5
                delivered[serial] += 1
                valid_pct[serial] = float((depth > 0).mean() * 100.0)
        dt = time.time() - t0

        want = max(1, frames)
        for serial, _ in drivers:
            got = delivered[serial]
            rate = got / dt if dt > 0 else 0.0
            tag = OK if got == want else FAIL
            _line(tag, f"serial {serial}: {got}/{want} framesets "
                       f"({rate:.1f} fps, depth valid {valid_pct[serial]:.1f}%)")

        total_rate = sum(delivered.values()) / dt if dt > 0 else 0.0
        _line(OK, f"aggregate {total_rate:.1f} framesets/s across {len(drivers)} camera(s)")
        # Per-camera rate falls as cameras are added because alignment is host-side CPU
        # work, not because the bus is full — worth separating from a bandwidth fault.
        if len(drivers) > 1:
            _line("      ", "per-camera fps drops as cameras are added: depth->colour "
                            "alignment is CPU-bound per stream, separate from bus limits")
        return 0
    finally:
        for serial, drv in drivers:
            try:
                drv.disconnect()
            except Exception:
                pass
        if drivers:
            _line(OK, f"disconnected {len(drivers)} camera(s) cleanly")


SETTLE_FRAMES = 5   # auto-exposure needs a few frames; frame 1 routinely reads saturated
PROBE_INDICES = 6   # fallback index count when device names cannot be enumerated

# A lab camera is a RealSense. Everything else AVFoundation offers on a MacBook — the
# built-in camera, Desk View, a Continuity iPhone, screen capture — is not part of the rig
# and must never back a fleet slot: it would point a verification agent at the operator's
# face or desktop instead of the bench, and (being always present) it makes a
# misconfiguration look like a working camera.
LAB_DEVICE_MARKER = "realsense"


def avfoundation_devices() -> "list[tuple[int, str]] | None":
    """[(index, name)] for AVFoundation video devices, or None if it cannot be determined.

    OpenCV addresses cameras by bare index and exposes no name, which makes an index
    meaningless on its own — index 1 was the bench's second camera one hour and the
    closed-lid MacBook camera the next. ffmpeg's avfoundation lister prints the same
    index->name table AVFoundation hands OpenCV, so it is what turns an index back into an
    identity. Shelling out to ffmpeg beats adding a PyObjC dependency for one lookup.
    """
    import re
    import subprocess

    try:
        # Listing devices is not a valid input, so ffmpeg exits non-zero by design and
        # writes the table to stderr. check=False, and the return code is ignored.
        proc = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, Exception):
        return None

    out: list[tuple[int, str]] = []
    in_video = False
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            break                      # audio indices restart at 0; do not mix them in
        if not in_video:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out or None


def is_lab_camera(name: str) -> bool:
    return LAB_DEVICE_MARKER in name.lower()


def describe_index(index: int, devices: "list[tuple[int, str]] | None") -> str:
    """Name for a UVC index, or a marker that it could not be identified."""
    if devices is None:
        return "unidentified (ffmpeg unavailable)"
    for i, name in devices:
        if i == index:
            return name
    return "no such device"


def stage_probe_uvc(count: int = PROBE_INDICES, persist: bool = True) -> int:
    """Identify each UVC index by name, and capture a frame from the lab cameras only.

    Indices alone are not identities: macOS renumbers them whenever a camera is added,
    removed or replugged, so a slot that worked can silently become a different device. This
    resolves index -> name first, then opens only the RealSense units.

    Non-lab devices (built-in MacBook camera, Desk View, Continuity iPhone, screen capture)
    are listed but never opened. Opening them would capture the operator or their desktop,
    and because they are always present they make a misconfigured slot look healthy.
    """
    import cv2

    from core.config import Settings
    from core.perception import save_frame

    root = Settings.load().capture_dir
    devices = avfoundation_devices()

    if devices is None:
        _line(WARN, "could not enumerate device names (ffmpeg missing) — falling back to "
                    "opening indices blind, which cannot tell a bench camera from the "
                    "built-in one. Install ffmpeg (`brew install ffmpeg`) for named output.")
        targets = list(range(count))
    else:
        print()
        for i, name in devices:
            tag = OK if is_lab_camera(name) else "      "
            note = "" if is_lab_camera(name) else "   (not a lab camera — skipped)"
            _line(tag, f"index {i}: {name}{note}")
        targets = [i for i, name in devices if is_lab_camera(name)]
        print()
        if not targets:
            _line(FAIL, "no RealSense camera is visible to AVFoundation")
            print(
                "       Nothing on this machine is a bench camera right now. Check the USB\n"
                "       connection, then confirm the OS sees the unit at all:\n"
                "         ioreg -p IOUSB -w0 -l | grep -i realsense\n"
                "       A D4xx that enumerates on USB but not here is usually on a port or\n"
                "       hub that did not bring up its UVC function — replug it directly."
            )
            return 6

    live, blank, dead = [], [], []
    for index in targets:
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            cap.release()
            dead.append(index)
            continue
        frame = None
        for _ in range(SETTLE_FRAMES):     # discard unsettled auto-exposure frames
            ok, f = cap.read()
            if ok:
                frame = f
        cap.release()
        if frame is None:
            dead.append(index)
            _line(FAIL, f"index {index}: opened but delivered no frame")
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = frame.shape[:2]
        kind = _frame_kind(frame)
        desc = (f"index {index}: {w}x{h}  {kind}  "
                f"mean={gray.mean():.1f} std={gray.std():.1f}")
        if kind == "BLANK":
            blank.append(index)
            _line(WARN, desc + "  <- shows nothing (covered lens or not streaming)")
        else:
            live.append(index)
            _line(OK, desc)

        if persist:
            # Fixed filename per index, not timestamped: this is a lookup table to glance at,
            # and accumulating probe frames would bury the current answer.
            written = save_frame("_probe", frame, root, stamp=f"index{index}", latest=False)
            _line("      ", f"-> {os.path.relpath(written[0])}")

    print()
    _line(OK if live else FAIL,
          f"{len(live)} lab camera(s) delivering {live}, {len(blank)} blank {blank}, "
          f"{len(dead)} unopenable {dead}")
    if persist and (live or blank):
        _line("      ", f"frames in {os.path.relpath(os.path.join(root, '_probe'))}/ — "
                        "look at them, then set CAM_<SLOT> to the matching index")
    print(
        "\n       An index is not an identity: macOS renumbers them whenever a camera is\n"
        "       added, removed or replugged, so a slot that worked can become a different\n"
        "       device with no config change. Re-run this after any change to the rig. To\n"
        "       bind to a physical unit instead, pin it by serial through the `realsense`\n"
        "       driver — that survives renumbering, at the cost of needing root on macOS."
    )
    return 0 if live else 6


def _frame_kind(frame) -> str:
    """"colour", "mono/IR", or "BLANK".

    A D4xx reached over plain UVC on macOS can present its *infrared* stream rather than the
    RGB module, and IR arrives as three identical channels — so a shape check calls it
    colour. Tag detection still works on IR, but there is no colour and no factory K.

    BLANK is checked *first* and deliberately: an all-black frame also has zero channel
    difference, so it would otherwise be reported as a healthy mono/IR camera. A device that
    opens and delivers empty frames is the dangerous case — it looks live while showing
    nothing, which is the one failure a verification rig must never wave through.

    Blankness is judged on the 99th percentile, not the standard deviation. Sensor noise on a
    black frame gives a std of ~3, which clears any threshold low enough to be safe; but the
    *brightest* pixels stay dark. Measured on this bench: a black frame ran mean 8.2, std 3.3,
    p99 15, max 16, while a real one ran mean 110, std 61, p99 255. The std ranges overlap
    with a dim-but-real scene, the percentiles do not. The second test catches a frozen
    uniform frame at any brightness, where p99 is high but nothing varies.
    """
    import numpy as np

    if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
        return "BLANK" if frame is None else "mono"
    gray = frame.mean(axis=2)
    if float(np.percentile(gray, 99)) < 24.0 or float(gray.std()) < 1.0:
        return "BLANK"
    b, g, r = (frame[:, :, i].astype(float) for i in range(3))
    identical = abs(b - g).mean() < 1.0 and abs(g - r).mean() < 1.0
    return "mono/IR" if identical else "colour"


def stage_fleet(frames: int, persist: bool = False) -> int:
    """Validate every camera slot in the configured fleet, all open at once.

    `--all` covers the RealSense path only. This one honours whatever driver each slot is
    actually configured to use (`CAM_<SLOT>_TYPE`), so it validates the rig the backend will
    really build — mixed `realsense` and plain `camera` slots included. That matters because
    on macOS the metric path needs root while the UVC path does not, so a real bench often
    runs a mix, and a RealSense-only check would either skip or wrongly fail those slots.
    """
    from core.config import Settings
    from drivers import build_driver

    entries = [e for e in Settings.load().fleet if e.get("type") in ("camera", "realsense")]
    if not entries:
        _line(FAIL, "no camera slots in the fleet")
        return 6

    _line(OK, f"{len(entries)} camera slot(s) configured")

    # Two slots resolving to the same device is the worst kind of misconfiguration here:
    # both open, both stream, and the rig reports one viewpoint twice under two names. A
    # `camera` slot with no source configured is part of this — the driver falls back to
    # index 0, so "unconfigured" and "the first camera" become indistinguishable.
    # Identity check, before anything is opened. This is the gate that a brightness test
    # cannot be: "a camera in a dark room" and "the wrong camera" look identical by
    # brightness, but a name says which physical device an index actually is.
    devices = avfoundation_devices()
    wrong_device = []
    for e in entries:
        if e.get("type") != "camera" or not isinstance(e.get("source"), int):
            continue
        eid = e.get("id", "?")
        name = describe_index(int(e["source"]), devices)
        if devices is not None and not is_lab_camera(name):
            wrong_device.append(eid)
            _line(FAIL, f"{eid}: source={e['source']} is \"{name}\" — not a bench camera")
        else:
            _line(OK, f"{eid}: source={e['source']} is \"{name}\"")
    if wrong_device:
        print(
            f"       {', '.join(wrong_device)} would stream a device that is not part of the\n"
            "       rig — the built-in camera, Desk View, a Continuity iPhone or the screen.\n"
            "       These are always present, so the slot looks healthy while showing the\n"
            "       operator or their desktop instead of the bench. Re-identify the indices:\n"
            "         .venv/bin/python scripts/validate_camera.py --probe-uvc"
        )

    seen: dict[tuple, list[str]] = {}
    for e in entries:
        eid, kind = e.get("id", "?"), e.get("type")
        if kind == "realsense":
            key = ("serial", str(e.get("serial") or ""))
        else:
            key = ("source", e.get("source") if "source" in e else 0)
            if "source" not in e:
                _line(WARN, f"{eid}: type is 'camera' but no source is set — the driver "
                            f"defaults to index 0, which is probably not what you want "
                            f"(set CAM_{eid.upper().replace('_CAM', '')} or drop the "
                            f"_TYPE override)")
        seen.setdefault(key, []).append(eid)
    for (what, value), ids in seen.items():
        if len(ids) > 1 and value not in ("",):
            _line(FAIL, f"{' and '.join(ids)} all resolve to {what}={value!r} — they would "
                        f"stream the SAME camera under different fleet ids")

    opened, failed = [], []
    try:
        for e in entries:
            eid, kind = e.get("id", "?"), e.get("type")
            # Refuse rather than open: capturing from the operator's own camera is not a
            # thing to do accidentally, and a frame from it is worse than no frame because
            # the rig would treat it as an observation of the bench.
            if eid in wrong_device:
                failed.append((eid, kind, RuntimeError("not a bench camera — refused")))
                _line(FAIL, f"{eid}: skipped, will not open a non-bench camera")
                continue
            pin = e.get("serial") or e.get("source")
            pin_desc = f"{'serial' if kind == 'realsense' else 'source'}={pin!r}" \
                if pin not in (None, "") else "unpinned"
            try:
                drv = build_driver(e)
                drv.connect()
            except Exception as exc:
                # One slot failing must not abort the others: the whole point is to see
                # which viewpoints are live, and a mixed rig routinely has some down.
                failed.append((eid, kind, exc))
                _line(FAIL, f"{eid} ({kind}, {pin_desc}): {exc}")
                continue
            opened.append((eid, kind, drv))
            _line(OK, f"{eid} ({kind}, {pin_desc}) open")

        if not opened:
            print()
            _explain_claim_failure(failed[0][2] if failed else RuntimeError("no cameras"))
            return 6

        for _ in range(SETTLE_FRAMES):          # let auto-exposure settle before judging
            for _eid, _kind, drv in opened:
                try:
                    drv.capture()
                except Exception:
                    pass

        delivered = {eid: 0 for eid, _, _ in opened}
        last = {}
        t0 = time.time()
        for _ in range(max(1, frames)):
            for eid, kind, drv in opened:
                try:
                    if getattr(drv, "has_depth", False):
                        color, depth = drv.capture_rgbd()
                    else:
                        color, depth = drv.capture(), None
                except Exception as exc:
                    _line(FAIL, f"{eid} stopped after {delivered[eid]} frame(s): {exc}")
                    return 6
                delivered[eid] += 1
                last[eid] = (color, depth)
        dt = time.time() - t0

        if persist:
            from core.config import Settings
            from core.perception import save_frame, timestamp

            root = Settings.load().capture_dir
            # One timestamp shared by the whole rig, so frames captured in the same window
            # carry the same filename across cameras and can be lined up afterwards.
            stamp = timestamp()
            print()
            for eid, _kind, _drv in opened:
                color, depth = last[eid]
                written = save_frame(eid, color, root, depth=depth, stamp=stamp)
                _line(OK, f"{eid:<14} saved {len(written)} file(s) -> "
                          f"{os.path.relpath(os.path.dirname(written[0]))}/")

        print()
        want = max(1, frames)
        blanks = []
        for eid, kind, drv in opened:
            color, depth = last[eid]
            got = delivered[eid]
            rate = got / dt if dt > 0 else 0.0
            h, w = color.shape[:2]
            kind_desc = _frame_kind(color)
            if kind_desc == "BLANK":
                blanks.append(eid)
            bits = [f"{w}x{h}", kind_desc, f"{got}/{want} frames",
                    f"{rate:.1f} fps"]
            if depth is not None:
                bits.append(f"depth valid {float((depth > 0).mean() * 100):.1f}%")
            else:
                bits.append("no depth")
            intr = drv.intrinsics() if hasattr(drv, "intrinsics") else None
            bits.append("factory K" if intr else "no intrinsics")
            # Frame count alone is not health: a blank slot delivers every frame asked for.
            healthy = got == want and kind_desc != "BLANK"
            _line(OK if healthy else FAIL, f"{eid:<14} " + "  ".join(bits))

        if blanks:
            _line(FAIL, f"{', '.join(blanks)} delivered uniform (blank) frames — the device "
                        f"opens and streams but shows nothing")
            print(
                "       Treat this as down, not up: it is the one failure that survives a\n"
                "       frame-count check, so anything downstream would 'verify' against an\n"
                "       empty image. Usually the index points at a closed-lid built-in camera\n"
                "       or a covered lens — re-identify what each index is with:\n"
                "         .venv/bin/python scripts/validate_camera.py --probe-uvc"
            )

        if any(_frame_kind(c) == "mono/IR" for c, _ in last.values()):
            _line("      ", "mono/IR slots are D4xx infrared over UVC: AprilTags are still "
                            "detectable, but the projector's dot pattern is superimposed and "
                            "there is no colour or factory K — so depth_m/camera_xyz stay null")
        if any(kind == "camera" for _eid, kind, _d in opened):
            _line("      ", "UVC `source` indices are assigned by macOS and shift when a "
                            "camera is added or replugged — re-check them after any change")
        return 0 if not failed and not blanks else 7
    finally:
        for eid, _kind, drv in opened:
            try:
                drv.disconnect()
            except Exception:
                pass
        if opened:
            _line(OK, f"disconnected {len(opened)} camera(s) cleanly")


def stage_save(last: dict, prefix: str) -> None:
    import cv2
    import numpy as np

    cpath, dpath = f"{prefix}_color.png", f"{prefix}_depth.png"
    cv2.imwrite(cpath, last["color"])
    # Scale metres -> 16-bit millimetres so the PNG stays lossless and inspectable.
    cv2.imwrite(dpath, (np.clip(last["depth"], 0, 65.535) * 1000.0).astype(np.uint16))
    _line(OK, f"wrote {cpath} and {dpath}")


def stage_fiducials(drv, last: dict) -> None:
    """Back-project tag detections with the factory intrinsics — the reason RGB-D
    cameras skip the ChArUco pass (FR-CAL-1)."""
    from core.perception import FiducialDetector

    dets = FiducialDetector(camera_matrix=drv.camera_matrix()).detect(last["color"])
    if not dets:
        _line(WARN, "no tag36h11 markers in view (expected if none are pointed at the camera)")
        return
    _line(OK, f"{len(dets)} marker(s): {sorted(d.marker_id for d in dets)}")
    depth = last["depth"]
    for d in dets:
        cx, cy = int(round(d.center[0])), int(round(d.center[1]))
        z = float(depth[cy, cx]) if 0 <= cy < depth.shape[0] and 0 <= cx < depth.shape[1] else 0.0
        pnp = f"{d.distance_m*1000:.0f}mm" if d.distance_m is not None else "n/a"
        _line("      ", f"id={d.marker_id} solvePnP={pnp} depth={z*1000:.0f}mm")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default=None, help="pin a specific unit by serial number")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--frames", type=int, default=1, help="framesets to pull (default 1)")
    ap.add_argument("--save", metavar="PREFIX", default=None,
                    help="write PREFIX_color.png and PREFIX_depth.png")
    ap.add_argument("--fiducials", action="store_true", help="run the tag36h11 detector")
    ap.add_argument("--list", action="store_true",
                    help="list SDK serials + a ready-to-paste .env block, then exit")
    ap.add_argument("--all", action="store_true",
                    help="validate every attached RealSense together (the three-camera rig)")
    ap.add_argument("--fleet", action="store_true",
                    help="validate the configured fleet as-is (mixed realsense/camera slots)")
    ap.add_argument("--persist", action="store_true",
                    help="save each camera's frame under temp/captures/<camera_id>/")
    ap.add_argument("--probe-uvc", action="store_true",
                    help="save a frame from every UVC index, to identify which is which")
    args = ap.parse_args()

    print("== RealSense validation ==")

    # Needs neither the RealSense SDK nor root: it is the fallback for working out what a
    # UVC index currently points at, which is exactly the state you are in when the SDK
    # path is unavailable or an index has silently renumbered.
    if args.probe_uvc:
        return stage_probe_uvc(persist=True)

    # --fleet honours each slot's configured driver, so it must not require the RealSense
    # SDK: a rig running every slot over plain UVC needs neither pyrealsense2 nor root.
    if args.fleet:
        return stage_fleet(args.frames, persist=args.persist)

    rs = stage_import()
    if rs is None:
        return 2
    devices = stage_enumerate(rs)
    if not devices:
        return 3

    if args.list:
        return stage_list(rs, devices)

    if args.all:
        serials = []
        for d in devices:
            try:
                serials.append(d.get_info(rs.camera_info.serial_number))
            except Exception as exc:
                _line(FAIL, f"could not read a serial: {exc}")
                _explain_claim_failure(exc)
                return 3
        print(f"\n-- all {len(serials)} camera(s) concurrently --")
        rc = stage_concurrent(serials, args.width, args.height, args.fps, args.frames)
        if rc == 0:
            print(f"\nAll {len(serials)} cameras stream together through the driver stack.")
        return rc

    drv, last = stage_stream(args.serial, args.width, args.height, args.fps, args.frames)
    try:
        if last is None:
            return 4
        if args.save:
            stage_save(last, args.save)
        if args.fiducials:
            stage_fiducials(drv, last)
    finally:
        if drv is not None:
            drv.disconnect()
            _line(OK, "disconnected cleanly")

    print("\nAll stages passed — the camera streams through the driver stack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
