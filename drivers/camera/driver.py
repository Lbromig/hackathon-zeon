"""OpenCV-backed camera driver (USB / on-arm / external webcams).

Also the root-free way to reach a RealSense on macOS: the D4xx exposes its IR
sensor as an ordinary UVC device, so this driver gets a live mono stream where
librealsense cannot open the camera at all. No depth, no factory intrinsics.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.camera import CameraDriver

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


class OpenCVCameraDriver(CameraDriver):
    """Config: {"source": 0, "name": "on_arm_left", "width": 1280, "height": 720}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._cap: Any = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.CAMERA,
            model="UVC camera",
            vendor="generic",
            meta={"source": self.config.get("source")},
        )

    def connect(self) -> None:
        if cv2 is None:
            raise DriverError("opencv-python not installed")
        self._state = ConnectionState.CONNECTING
        source = self.config.get("source", 0)
        # A UVC device admits exactly one process. The common cause of a failed
        # open is our own previous backend still holding it — uvicorn --reload in
        # particular overlaps old and new workers for a moment — so retry briefly
        # before giving up, and name that cause, because OpenCV's own answer is a
        # bare False with no reason attached.
        attempts = int(self.config.get("open_attempts", 3))
        for attempt in range(attempts):
            self._cap = cv2.VideoCapture(source)
            if self._cap.isOpened():
                break
            self._cap.release()
            if attempt < attempts - 1:
                time.sleep(0.7)
        else:
            self._state = ConnectionState.ERROR
            raise DriverError(
                f"camera source {source!r} would not open after {attempts} attempts — "
                "another process is probably holding it (a previous backend, Photo Booth, "
                "or a browser tab), or this process lacks macOS camera permission"
            )

        if self.config.get("width"):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config["width"])
        if self.config.get("height"):
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config["height"])

        # Discard the first frames: auto-exposure starts wide open, so a cold
        # camera's first frame is often a white-out that detects no markers and
        # looks broken in the UI. Measured on a D435 IR node: ~237 mean brightness
        # settling to ~105 within a dozen frames.
        for _ in range(int(self.config.get("warmup_frames", 10))):
            if not self._cap.read()[0]:
                break
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        if self._cap is not None:
            self._cap.release()
        self._cap = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._cap is not None and self._cap.isOpened()}

    def capture(self) -> Any:
        if self._cap is None:
            raise DriverError("camera not connected")
        ok, frame = self._cap.read()
        if not ok:
            raise DriverError("frame grab failed")
        return frame

    def capture_jpeg(self, quality: int = 85) -> bytes:
        ok, buf = cv2.imencode(".jpg", self.capture(), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise DriverError("jpeg encode failed")
        return buf.tobytes()
