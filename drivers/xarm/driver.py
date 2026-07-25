"""xArm Lite 6 driver — wraps the vendored xArm-Python-SDK.

We run two of these (left / right) cooperatively. Both are calibrated into one
shared world frame; that transform lives in config (``world_from_base``).
"""
from __future__ import annotations

from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.arm import ArmDriver, Pose

try:  # SDK is optional at import time so the backend can boot without hardware
    from xarm.wrapper import XArmAPI
except Exception:  # pragma: no cover
    XArmAPI = None


class XArmDriver(ArmDriver):
    """Config: {"ip": "192.168.1.xxx", "name": "left", "tcp_speed": 120}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._api: "XArmAPI | None" = None
        self._ip = self.config.get("ip")

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.ARM,
            model="xArm Lite 6",
            vendor="UFactory",
            meta={"ip": self._ip},
        )

    def connect(self) -> None:
        if XArmAPI is None:
            raise DriverError("xArm SDK not installed (pip install -e third_party/xArm-Python-SDK)")
        if not self._ip:
            raise DriverError("xArm driver requires config['ip']")
        self._state = ConnectionState.CONNECTING
        try:
            self._api = XArmAPI(self._ip)
            self._api.motion_enable(True)
            self._api.set_mode(0)
            self._api.set_state(0)
            self._state = ConnectionState.CONNECTED
        except Exception as e:  # pragma: no cover
            self._state = ConnectionState.ERROR
            raise DriverError(f"xArm connect failed: {e}") from e

    def disconnect(self) -> None:
        if self._api is not None:
            self._api.disconnect()
        self._api = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        if self._api is None:
            return {"state": self._state, "connected": False}
        return {
            "state": self._state,
            "connected": True,
            "pose": self.get_pose().__dict__,
            "error_code": self._api.error_code,
            "warn_code": self._api.warn_code,
        }

    # --- motion ------------------------------------------------------------
    def _require(self) -> "XArmAPI":
        if self._api is None:
            raise DriverError("xArm not connected")
        return self._api

    def enable(self, on: bool = True) -> None:
        api = self._require()
        api.motion_enable(on)
        api.set_state(0 if on else 4)

    def home(self) -> None:
        self._require().move_gohome(wait=True)

    def get_pose(self) -> Pose:
        code, p = self._require().get_position(is_radian=False)
        if code != 0:
            raise DriverError(f"get_position error {code}")
        return Pose(*p)  # [x, y, z, roll, pitch, yaw]

    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None:
        self._require().set_position(
            x=pose.x, y=pose.y, z=pose.z,
            roll=pose.roll, pitch=pose.pitch, yaw=pose.yaw,
            speed=speed or self.config.get("tcp_speed", 100), wait=wait,
        )

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0, wait=True) -> None:
        self._require().set_position(
            x=dx, y=dy, z=dz, roll=droll, pitch=dpitch, yaw=dyaw,
            relative=True, wait=wait, speed=self.config.get("tcp_speed", 100),
        )

    # --- gripper -----------------------------------------------------------
    def grip(self, width: float | None = None, force: float | None = None) -> None:
        api = self._require()
        api.set_gripper_enable(True)
        api.set_gripper_position(width if width is not None else 0, wait=True)

    def release(self) -> None:
        api = self._require()
        api.set_gripper_enable(True)
        api.set_gripper_position(850, wait=True)  # open

    def gripper_width(self) -> float:
        code, pos = self._require().get_gripper_position()
        if code != 0:
            raise DriverError(f"get_gripper_position error {code}")
        return float(pos)
