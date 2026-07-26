"""Capture one frame from every camera slot at backend startup.

Why on every boot: a camera slot is a name pointing at a device index, and on macOS those
indices are reassigned whenever the rig changes. A slot can therefore come up pointing at a
different camera — or at nothing — with no config change and no error. A frame written at
boot turns that from an invisible drift into something you can look at: open
`temp/captures/<slot>/latest_color.png` and you know what that slot actually got this run.

It also gives every session a dated first frame per viewpoint, which is the reference you
want when a later detection looks wrong and the question is whether the camera moved.

Runs on a daemon thread, deliberately. Opening a UVC device on macOS can block in an
uninterruptible kernel wait — observed on this bench, where a stuck capture left processes
that `kill -9` could not reap. Boot must not be able to hang on a camera, so nothing here is
awaited: the API comes up regardless and the snapshots land when they land.

Disable with HZ_STARTUP_SNAPSHOT=0.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from drivers import ConnectionState, InstrumentKind

from core.obs import get_logger

log = get_logger(__name__)

# Frames to pull and throw away before keeping one. The first frames off a UVC camera are
# auto-exposure settling and routinely come back saturated or black — a snapshot taken from
# frame 1 would misrepresent a perfectly good camera.
SETTLE_FRAMES = 5


def _is_camera(driver: Any) -> bool:
    try:
        return driver.info.kind == InstrumentKind.CAMERA
    except Exception:
        return False


def _snapshot_one(driver: Any, root: str) -> tuple[str, str]:
    """(camera_id, outcome) for one slot. Never raises — one dead camera must not stop
    the others being recorded."""
    from core.perception import save_frame

    cam_id = driver.device_id
    # Track whether we were the one to open it: a slot the operator had already connected
    # must be left connected, and one we opened ourselves must not be left holding the
    # device away from camera_hub's worker.
    opened_here = False
    try:
        if getattr(driver, "_state", None) != ConnectionState.CONNECTED:
            driver.connect()
            opened_here = True

        for _ in range(SETTLE_FRAMES):
            try:
                driver.capture()
            except Exception:
                pass

        depth = None
        if getattr(driver, "has_depth", False):
            try:
                color, depth = driver.capture_rgbd()
            except Exception:
                color = driver.capture()
        else:
            color = driver.capture()

        written = save_frame(cam_id, color, root, depth=depth)
        return cam_id, f"ok ({len(written)} file(s))"
    except Exception as exc:
        return cam_id, f"unavailable: {exc}"
    finally:
        if opened_here:
            try:
                driver.disconnect()
            except Exception:
                pass


def run(blocking: bool = False) -> threading.Thread | None:
    """Snapshot every camera slot. Returns the worker thread, or None if disabled."""
    if os.getenv("HZ_STARTUP_SNAPSHOT", "1").strip().lower() in ("0", "false", "no"):
        log.info("disabled (HZ_STARTUP_SNAPSHOT)")
        return None

    # Never under pytest. Every TestClient enters the app lifespan, so without this the
    # suite opens the real cameras once per test — which is slow, fights the camera_hub
    # tests for the device, and makes results depend on what is plugged into the bench.
    # Checked here rather than left to a conftest fixture: a test run touching hardware is
    # a correctness problem, and it must not depend on each conftest remembering to opt out.
    import sys
    if "pytest" in sys.modules:
        return None

    def _work() -> None:
        from core.config import settings
        from .device_manager import device_manager

        cameras = [d for d in device_manager.all() if _is_camera(d)]
        if not cameras:
            log.warning("no camera slots in the fleet")
            return

        root = settings.capture_dir
        log.info("capturing %d camera slot(s) -> %s", len(cameras), root)
        for driver in cameras:
            cam_id, outcome = _snapshot_one(driver, root)
            log.info("%s: %s", cam_id, outcome, extra={"device": cam_id,
                                                       "event": "camera_event"})

    if blocking:
        _work()
        return None
    thread = threading.Thread(target=_work, name="startup-snapshot", daemon=True)
    thread.start()
    return thread
