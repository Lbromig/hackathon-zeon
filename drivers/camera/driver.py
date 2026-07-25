"""OpenCV-backed camera driver (USB / on-arm / external webcams)."""
from __future__ import annotations

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
        self._cap = cv2.VideoCapture(self.config.get("source", 0))
        if self.config.get("width"):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config["width"])
        if self.config.get("height"):
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config["height"])
        if not self._cap.isOpened():
            self._state = ConnectionState.ERROR
            raise DriverError("camera failed to open")
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
