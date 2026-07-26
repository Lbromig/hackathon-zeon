"""Serve a saved frame as if it were a live camera.

Why this is not just `cv2.VideoCapture("frame.png")`: OpenCV opens an image file as a
one-frame video, so the first read succeeds and every read after it fails. A camera slot
backed that way delivers a single frame and then drops into permanent error — which looks
like a flaky camera rather than a still.

The real use is a viewpoint whose hardware is temporarily unavailable: the arm camera gets
unplugged and used elsewhere, but the rest of the stack still needs that slot to exist so
detection, the Cameras tab and the world model keep working against its last known view.

Config: {"source": "<path to .png>", "name": "..."}
If a sibling `*_depth.png` exists (as written by core.perception.save_frame) it is loaded
too and the driver reports has_depth, so RGB-D consumers work against the replayed frame.

This driver is honest about being a still: `info.meta` marks it, so anything downstream can
tell a replayed frame from a live one. A verification agent must never mistake a stored
image for a current observation — the frame does not change, so it can "confirm" a state
that has since gone away.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.camera import CameraDriver

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


class StillImageCameraDriver(CameraDriver):
    """A camera whose frames come from a file on disk."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._path: str = ""
        self._depth_path: str | None = None
        self._frame: Any = None
        self._depth: Any = None
        self._mtime: float = 0.0

    @property
    def has_depth(self) -> bool:                      # type: ignore[override]
        return self._depth is not None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.CAMERA,
            model="Still image",
            vendor="file",
            # `live: False` is the important field — it lets the UI and any verification
            # step distinguish a replayed frame from a real observation.
            meta={"source": self._path, "live": False, "depth": self.has_depth},
        )

    def connect(self) -> None:
        if cv2 is None:
            raise DriverError("opencv not installed")
        path = str(self.config.get("source") or "")
        if not path:
            raise DriverError("no source path configured")
        if not os.path.exists(path):
            # A bare number here means the slot's *_TYPE says `still` while its source is
            # still a UVC index. That pairing is the likely mistake, and "image not found:
            # 2" on its own sends you looking for a missing file instead.
            if path.strip().lstrip("-").isdigit():
                raise DriverError(
                    f"source {path!r} is a device index, not an image path — this slot is "
                    f"configured as type 'still'. Either set the source to a file path, or "
                    f"set the slot's *_TYPE to 'camera' to open index {path} live."
                )
            raise DriverError(f"image not found: {path}")

        self._state = ConnectionState.CONNECTING
        self._path = path
        # save_frame writes "<stamp>_color.png" alongside "<stamp>_depth.png"; pair them
        # when both exist so a replayed slot can still answer depth queries.
        if path.endswith("_color.png"):
            sibling = path[: -len("_color.png")] + "_depth.png"
            self._depth_path = sibling if os.path.exists(sibling) else None
        self._load()
        self._state = ConnectionState.CONNECTED

    def _load(self) -> None:
        frame = cv2.imread(self._path)
        if frame is None:
            raise DriverError(f"could not decode image: {self._path}")
        self._frame = frame
        self._mtime = os.path.getmtime(self._path)
        if self._depth_path:
            # IMREAD_UNCHANGED or the 16-bit millimetres collapse to 8-bit and the
            # measurement becomes noise.
            raw = cv2.imread(self._depth_path, cv2.IMREAD_UNCHANGED)
            self._depth = None if raw is None else raw.astype(np.float32) / 1000.0

    def _maybe_reload(self) -> None:
        """Pick up an edited file without a reconnect — drop in a new frame and the slot
        follows it, which is what makes this usable while staging a demo."""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return                                     # file vanished; keep serving the last
        if mtime != self._mtime:
            self._load()

    def disconnect(self) -> None:
        self._frame = None
        self._depth = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._frame is not None,
                "depth": self.has_depth, "live": False, "source": self._path}

    def capture(self) -> Any:
        if self._frame is None:
            raise DriverError("camera not connected")
        self._maybe_reload()
        # A copy per call: callers annotate frames in place (the detector overlay does),
        # and handing out the cached array would let one consumer scribble on every
        # subsequent read.
        return self._frame.copy()

    def capture_depth(self) -> Any:
        if self._depth is None:
            raise DriverError(f"{self.device_id}: no depth image alongside {self._path}")
        self._maybe_reload()
        return self._depth.copy()

    def capture_rgbd(self) -> tuple[Any, Any]:
        return self.capture(), self.capture_depth()

    def capture_jpeg(self, quality: int = 85) -> bytes:
        ok, buf = cv2.imencode(".jpg", self.capture(), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise DriverError("jpeg encode failed")
        return buf.tobytes()
