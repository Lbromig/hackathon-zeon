"""Persist camera frames to disk, one subfolder per camera.

Why this exists: with a multi-camera rig the first question is always "which physical
viewpoint is this fleet id?", and that is answered by looking at a picture, not by reading
a serial. Writing every camera's frames to its own folder makes the rig inspectable — you
open `temp/captures/overview_cam/latest_color.png` and you know what `overview_cam` sees.
It is also the record for after the fact: a verification step that claimed a cap was off
leaves behind the frame it decided on.

Layout (root is gitignored, see Settings.capture_dir):

    temp/captures/
      gripper_cam/
        latest_color.png              # overwritten every save, for quick eyeballing
        20260725T173012_486Z_color.png
        20260725T173012_486Z_depth.png
      overview_cam/
        ...

Filenames are UTC timestamps to millisecond precision, so saves accumulate rather than
overwrite and sort chronologically as plain strings. Depth is written as 16-bit PNG in
**millimetres**, not a colourised preview: the measurement is the reason for an RGB-D
camera, and a colourmap throws it away.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import cv2
import numpy as np

# 65.535 m is the ceiling of a uint16 in millimetres. Real D4xx range is far below this;
# the clip only guards against the sentinel/garbage values a bad frame can carry, which
# would otherwise wrap around and read as a *near* measurement instead of a broken one.
_MAX_DEPTH_M = 65.535


def timestamp() -> str:
    """UTC, millisecond precision, safe for filenames and sorts as a string."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")[:-3] + "Z"


def camera_dir(camera_id: str, root: str) -> str:
    """The subfolder for one camera, created on demand.

    The id is sanitised because it reaches here from fleet config, which is user-editable:
    a slash or a `..` in a fleet id would otherwise write outside the capture root.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in camera_id) or "unknown"
    path = os.path.join(root, safe)
    os.makedirs(path, exist_ok=True)
    return path


def save_frame(camera_id: str, color: "np.ndarray | None", root: str,
               depth: "np.ndarray | None" = None, *,
               stamp: str | None = None, latest: bool = True) -> list[str]:
    """Write one camera's frame(s) into its own subfolder. Returns the paths written.

    `depth` is in metres (what CameraDriver.capture_depth returns) and is converted to
    16-bit millimetres here. Passing only `color` is normal — the UVC path has no depth.
    """
    out: list[str] = []
    if color is None and depth is None:
        return out

    folder = camera_dir(camera_id, root)
    stamp = stamp or timestamp()

    if color is not None:
        path = os.path.join(folder, f"{stamp}_color.png")
        cv2.imwrite(path, color)
        out.append(path)
        if latest:
            # A copy rather than a symlink: this folder gets opened by Finder, browsers and
            # scp, and a symlink is a broken image to some of them.
            latest_path = os.path.join(folder, "latest_color.png")
            cv2.imwrite(latest_path, color)
            out.append(latest_path)

    if depth is not None:
        mm = (np.clip(depth, 0, _MAX_DEPTH_M) * 1000.0).astype(np.uint16)
        path = os.path.join(folder, f"{stamp}_depth.png")
        cv2.imwrite(path, mm)
        out.append(path)

    return out


def load_depth_mm(path: str) -> "np.ndarray":
    """Read a depth PNG written by save_frame back into metres.

    Needed because cv2.imread drops 16-bit data to 8-bit unless told otherwise, which
    silently turns a millimetre measurement into noise.
    """
    raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise FileNotFoundError(path)
    return raw.astype(np.float32) / 1000.0
