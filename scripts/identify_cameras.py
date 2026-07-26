#!/usr/bin/env python3
"""Identify every bench camera by a STABLE key, and snapshot each one.

Why this exists: an OpenCV/AVFoundation index is not an identity. macOS renumbers video
devices whenever one is added, removed, replugged — and, measured on this bench, simply
between two consecutive `ffmpeg -list_devices` calls seconds apart, with nothing touched.
A slot that worked can silently become a *different* camera, or the operator's face.

The stable key is AVFoundation's **uniqueID**, whose high bytes are the USB `locationID`:

    0x124300080860b5b
      ^^^^ locationID 0x01243000 -> the physical USB port
                                 -> ioreg gives that port's USB serial number

So uniqueID identifies a physical port, and the USB serial identifies the unit plugged
into it. Both survive renumbering, and neither needs root (unlike librealsense, which is
the other way to bind by serial and needs root on macOS to claim the UVC interface).

Capture therefore goes through `scripts/avfsnap.swift` (built on demand), which calls
`AVCaptureDevice(uniqueID:)` and fails loudly when there is no such device.

Do NOT be tempted to capture with ffmpeg here. Measured on this bench:

  * `ffmpeg -f avfoundation -i "<exact device name>"` does bind correctly, and an
    unknown name is refused — but the two D405s share one name, so it cannot separate them.
  * `ffmpeg -f avfoundation -i "<uniqueID>"` does NOT match the uniqueID. It silently
    opens the default device instead, so you get a plausible frame from the wrong camera
    with a zero exit status. A bogus uniqueID also "succeeds". That failure mode is why
    every frame here is fingerprinted (colour vs IR, pairwise difference) rather than
    trusted.

Apple's own cameras (built-in, Desk View, Continuity iPhone) are listed but NEVER opened:
they are always present, so a misconfigured slot backed by one looks healthy while
pointing a verification agent at the operator instead of the bench.

    .venv/bin/python scripts/identify_cameras.py             # table only
    .venv/bin/python scripts/identify_cameras.py --snapshot  # + one frame each
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A lab camera is a RealSense. Everything else on this Mac is an Apple camera pointed at a
# person or a desktop; see the module docstring.
LAB_MARKER = "realsense"
DEFAULT_W, DEFAULT_H = 1280, 720


def avf_cameras() -> list[dict]:
    """[{name, unique_id}] from system_profiler, which reports uniqueID (ffmpeg does not)."""
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=60, check=False).stdout
    except Exception as exc:                                    # noqa: BLE001
        print(f"system_profiler failed: {exc}", file=sys.stderr)
        return []

    cams, name = [], None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and stripped != "Camera:" and "Model ID" not in stripped:
            name = stripped[:-1]
        elif stripped.startswith("Unique ID:") and name:
            cams.append({"name": name, "unique_id": stripped.split(":", 1)[1].strip()})
            name = None
    return cams


def usb_serials() -> dict[str, dict]:
    """{locationID_hex: {product, serial}} for every USB device, from ioreg.

    locationID is what links a camera's AVFoundation uniqueID to the USB unit behind it.
    """
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                             capture_output=True, text=True, timeout=60, check=False).stdout
    except Exception:                                           # noqa: BLE001
        return {}

    devices, cur = {}, {}
    for line in out.splitlines():
        m = re.search(r"\+-o (.+?)@([0-9a-fA-F]+)\s", line)
        if m:
            if cur.get("loc"):
                devices[cur["loc"]] = cur
            cur = {"product": m.group(1), "loc": m.group(2).lower().lstrip("0"), "serial": None}
            continue
        m = re.search(r'"USB Serial Number" = "(.+?)"', line)
        if m and cur:
            cur["serial"] = m.group(1)
    if cur.get("loc"):
        devices[cur["loc"]] = cur
    return devices


def location_of(unique_id: str) -> str:
    """uniqueID -> the USB locationID it embeds ('0x124300080860b5b' -> '1243000')."""
    hexpart = unique_id[2:] if unique_id.lower().startswith("0x") else unique_id
    return hexpart[:4].lstrip("0") + "000"


def ffmpeg_order() -> dict[str, int]:
    """{name: index} in ffmpeg's current enumeration order — for reference only.

    Deliberately NOT used to select a device: this order is what proved unstable. It is
    printed so a stale CAM_* index in .env can be recognised as stale.
    """
    try:
        proc = subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true",
                               "-i", ""], capture_output=True, text=True, timeout=60,
                              check=False)
    except Exception:                                           # noqa: BLE001
        return {}
    order, in_video = {}, False
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            break
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line) if in_video else None
        if m:
            order.setdefault(m.group(2), int(m.group(1)))
    return order


def avfsnap_binary() -> str | None:
    """Path to the avfsnap helper, compiling it from scripts/avfsnap.swift on first use."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "avfsnap.swift")
    out = os.path.join(os.path.dirname(here), "temp", "bin", "avfsnap")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    proc = subprocess.run(["swiftc", "-O", src, "-o", out],
                          capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        print(f"could not build avfsnap:\n{proc.stderr.strip()[:400]}", file=sys.stderr)
        return None
    return out


def snapshot(unique_id: str, dest: str, width: int, height: int) -> str | None:
    """Grab one settled frame from the device with this uniqueID. Returns the path."""
    binary = avfsnap_binary()
    if binary is None:
        return None
    proc = subprocess.run([binary, "grab", unique_id, dest, str(width), str(height)],
                          capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0 or not os.path.exists(dest):
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        print(f"      capture failed: {err[-1] if err else 'no frame'}")
        return None
    return dest


def describe(path: str) -> str:
    """Content fingerprint: resolution + colour-vs-IR + exposure, for eyeball-free triage."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return ""
    im = cv2.imread(path)
    if im is None:
        return ""
    b, g, r = cv2.split(im.astype("int16"))
    # A D4xx reached over UVC often presents its INFRARED stream, which arrives as three
    # identical channels -- so a shape check calls it colour. Channel difference does not.
    chan = float(np.mean(np.abs(b - g)) + np.mean(np.abs(g - r)))
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    kind = "colour" if chan > 2 else "mono/IR"
    return (f"{im.shape[1]}x{im.shape[0]} {kind} "
            f"mean={gray.mean():.0f} std={gray.std():.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="store_true", help="capture one frame per camera")
    ap.add_argument("--out", default=None, help="output dir (default <capture_dir>/_inventory)")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    args = ap.parse_args()

    cams = avf_cameras()
    if not cams:
        print("No cameras reported by system_profiler.")
        return 6
    usb, order = usb_serials(), ffmpeg_order()

    out_dir = args.out
    if out_dir is None:
        try:
            from core.config import Settings
            out_dir = os.path.join(Settings.load().capture_dir, "_inventory")
        except Exception:                                       # noqa: BLE001
            out_dir = os.path.join("temp", "captures", "_inventory")
    if args.snapshot:
        os.makedirs(out_dir, exist_ok=True)

    lab = [c for c in cams if LAB_MARKER in c["name"].lower()]
    skipped = [c for c in cams if c not in lab]

    print(f"\n{len(lab)} lab camera(s), {len(skipped)} Apple camera(s) ignored\n")
    rc = 0
    for cam in lab:
        loc = location_of(cam["unique_id"])
        entry = usb.get(loc, {})
        serial = entry.get("serial") or "?"
        idx = order.get(cam["name"])
        print(f"  {cam['name']}")
        print(f"    uniqueID     {cam['unique_id']}       <- bind to THIS")
        print(f"    USB serial   {serial}")
        print(f"    USB location 0x0{loc}")
        print(f"    ffmpeg index {idx if idx is not None else '?'}  "
              "(unstable — reference only, never bind to it)")
        if args.snapshot:
            model = re.sub(r"[^A-Za-z0-9]+", "_", cam["name"]).strip("_")
            dest = os.path.join(out_dir, f"{serial}_{model}.png")
            got = snapshot(cam["unique_id"], dest, args.width, args.height)
            if got:
                print(f"    frame        {os.path.relpath(got)}  {describe(got)}")
            else:
                rc = 7
        print()

    for cam in skipped:
        print(f"  ignored: {cam['name']}  ({cam['unique_id']})")

    dupes = [c["name"] for c in lab if [x["name"] for x in lab].count(c["name"]) > 1]
    if dupes:
        print(f"\nNote: {len(set(dupes))} name(s) are shared by more than one unit "
              f"({sorted(set(dupes))[0]!r}).\n"
              "Binding by name would hit whichever enumerates first — use the uniqueID.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
