"""Intel RealSense (RGB-D) camera driver via pyrealsense2.

All three lab cameras are RealSense, so every viewpoint has aligned colour + metric depth
and factory intrinsics. That means:
  - intrinsics() is available without a ChArUco pass (simplifies FR-CAL-1),
  - FoundationPose can run in RGB-D mode on any camera,
  - classical CV / AprilTag detections can be back-projected straight to 3D (metres).

Config: {"serial": "<optional>", "width": 1280, "height": 720, "fps": 30}
Depth is aligned to the colour frame so colour pixels and depth share coordinates.
pyrealsense2 is imported lazily so the rest of the stack runs without it installed.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.camera import CameraDriver

try:
    import pyrealsense2 as rs
except Exception:  # pragma: no cover - hardware/SDK not present in all envs
    rs = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


class RealSenseCameraDriver(CameraDriver):
    has_depth = True

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._pipe: Any = None
        self._align: Any = None
        self._depth_scale: float = 1.0
        self._intr: dict[str, Any] | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.CAMERA,
            model="Intel RealSense",
            vendor="Intel",
            meta={"serial": self.config.get("serial"), "depth": True},
        )

    def connect(self) -> None:
        if rs is None:
            raise DriverError("pyrealsense2 not installed")
        w = int(self.config.get("width", 1280))
        h = int(self.config.get("height", 720))
        fps = int(self.config.get("fps", 30))
        cfg = rs.config()
        if self.config.get("serial"):
            cfg.enable_device(str(self.config["serial"]))
        cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)

        self._state = ConnectionState.CONNECTING
        try:
            # Ask a child whether the claim can succeed before doing it here.
            # pipe.start() is the call that faults when UVCAssistant holds the
            # interfaces, and a fault in this process kills the whole backend.
            # Refusing with a reason is recoverable; crashing is not.
            from core.perception.rs_devices import can_claim

            ok, why = can_claim(str(self.config.get("serial", "")))
            if not ok:
                self._state = ConnectionState.ERROR
                raise DriverError(f"cannot claim camera: {why}")

            self._pipe = rs.pipeline()
            profile = self._pipe.start(cfg)
            self._align = rs.align(rs.stream.color)          # align depth -> colour
            self._depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()

            # factory intrinsics of the colour stream (post-alignment reference frame)
            vsp = profile.get_stream(rs.stream.color).as_video_stream_profile()
            i = vsp.get_intrinsics()
            self._intr = {
                "fx": i.fx, "fy": i.fy, "cx": i.ppx, "cy": i.ppy,
                "width": i.width, "height": i.height, "coeffs": list(i.coeffs),
            }
        except Exception as e:
            # librealsense raises bare RuntimeErrors ("No device connected",
            # "failed to set power state"). Callers above this layer only know
            # DriverError, so an unwrapped one surfaces as a 500 instead of "your
            # camera is unplugged". Reset state too: leaving it CONNECTING makes a
            # failed open look like one still in progress, forever.
            self._pipe = None
            self._state = ConnectionState.ERROR
            raise DriverError(f"RealSense {self.device_id} failed to start: {e}") from e
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.stop()
            except Exception:
                pass
        self._pipe = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._pipe is not None, "depth": True}

    # --- frames ----------------------------------------------------------------
    def _frames(self):
        if self._pipe is None:
            raise DriverError("camera not connected")
        frames = self._align.process(self._pipe.wait_for_frames())
        return frames.get_color_frame(), frames.get_depth_frame()

    def capture(self) -> Any:
        color, _ = self._frames()
        if not color:
            raise DriverError("no colour frame")
        return np.asanyarray(color.get_data())          # HxWx3 BGR

    def capture_depth(self) -> Any:
        _, depth = self._frames()
        if not depth:
            raise DriverError("no depth frame")
        return np.asanyarray(depth.get_data()).astype(np.float32) * self._depth_scale  # metres

    def capture_rgbd(self) -> tuple[Any, Any]:
        color, depth = self._frames()
        if not color or not depth:
            raise DriverError("incomplete RGB-D frameset")
        return (np.asanyarray(color.get_data()),
                np.asanyarray(depth.get_data()).astype(np.float32) * self._depth_scale)

    def intrinsics(self) -> dict[str, Any] | None:
        return dict(self._intr) if self._intr else None

    def capture_jpeg(self, quality: int = 85) -> bytes:
        if cv2 is None:
            raise DriverError("opencv not installed")
        ok, buf = cv2.imencode(".jpg", self.capture(), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise DriverError("jpeg encode failed")
        return buf.tobytes()

    def camera_matrix(self) -> Any:
        """3x3 K for solvePnP / FiducialDetector, from factory intrinsics."""
        i = self._intr
        if not i:
            raise DriverError("intrinsics unavailable (connect first)")
        return np.array([[i["fx"], 0, i["cx"]], [0, i["fy"], i["cy"]], [0, 0, 1]], float)
