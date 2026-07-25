"""In-memory mock drivers — hardware-free stand-ins for tests and demos.

Importing this module registers the mocks under their own type names
(``mock_arm`` / ``mock_camera`` / ``mock_liquid_handler``) so a fleet config can
reference them exactly like a real driver. No vendor SDK, cv2, or sockets — every
call updates plain in-memory state, so the backend and workflows can run end to
end on a laptop.
"""
from __future__ import annotations

from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.arm import ArmDriver, GripperInfo, Pose
from ..capabilities.camera import CameraDriver
from ..capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver
from ..registry import register


class MockArmDriver(ArmDriver):
    """A 6-axis arm that just remembers where it was told to go."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._pose = Pose(200.0, 0.0, 200.0, 180.0, 0.0, 0.0)
        self._joints = [0.0] * self.axis_count
        self._gripper = 850.0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.ARM, model="MockArm", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED,
                "error_code": 0, "warn_code": 0}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def enable(self, on: bool = True) -> None:
        self._state = ConnectionState.CONNECTED if on else ConnectionState.DISCONNECTED

    def home(self) -> None:
        self._joints = [0.0] * self.axis_count
        self._pose = Pose(200.0, 0.0, 200.0, 180.0, 0.0, 0.0)

    def stop(self, emergency: bool = False) -> None:
        pass

    def clear_errors(self) -> None:
        pass

    def get_pose(self) -> Pose:
        return Pose(**self._pose.__dict__)

    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None:
        self._pose = Pose(**pose.__dict__)

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0,
                      speed: float | None = None, wait: bool = True) -> None:
        p = self._pose
        self._pose = Pose(p.x + dx, p.y + dy, p.z + dz,
                          p.roll + droll, p.pitch + dpitch, p.yaw + dyaw)

    def get_joints(self) -> list[float]:
        return list(self._joints)

    def move_joints(self, angles: list[float], speed: float | None = None,
                    wait: bool = True) -> None:
        self._joints = [float(a) for a in angles]

    def move_joints_relative(self, deltas: list[float], speed: float | None = None,
                             wait: bool = True) -> None:
        self._joints = [a + d for a, d in zip(self._joints, deltas)]

    @property
    def gripper_info(self) -> GripperInfo:
        return GripperInfo(kind="parallel", supports_width=True, min_width=0.0,
                           max_width=850.0, units="counts", stroke_m=0.085)

    def grip(self, width: float | None = None, force: float | None = None) -> None:
        if force is not None:
            raise ValueError("mock gripper has no force control")
        self._gripper = 0.0 if width is None else float(width)

    def release(self) -> None:
        self._gripper = 850.0

    def gripper_width(self) -> float | None:
        return self._gripper


class MockCameraDriver(CameraDriver):
    """A camera that returns a tiny synthetic frame."""

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.CAMERA, model="MockCamera", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def capture(self) -> Any:
        try:  # numpy is a backend dep, but keep the mock importable without it
            import numpy as np

            return np.zeros((4, 4, 3), dtype="uint8")
        except Exception:  # pragma: no cover
            return b"\x00" * 48

    def capture_jpeg(self, quality: int = 85) -> bytes:
        return b"\xff\xd8\xff\xd9"  # minimal JPEG (SOI + EOI markers)


class MockLiquidHandlerDriver(LiquidHandlerDriver):
    """A liquid handler that tracks tip state and aspirated volume."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._has_tip = False
        self._aspirated_ul = 0.0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.LIQUID_HANDLER, model="MockLH", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED,
                "has_tip": self._has_tip, "aspirated_ul": self._aspirated_ul}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def home(self) -> None:
        pass

    def pick_up_tip(self, location: DeckLocation) -> None:
        self._has_tip = True

    def drop_tip(self, location: DeckLocation | None = None) -> None:
        self._has_tip = False

    def aspirate(self, volume_ul: float, location: DeckLocation) -> None:
        self._aspirated_ul += float(volume_ul)

    def dispense(self, volume_ul: float, location: DeckLocation) -> None:
        self._aspirated_ul = max(0.0, self._aspirated_ul - float(volume_ul))

    def move_to(self, location: DeckLocation) -> None:
        pass


def register_mocks() -> None:
    """(Re)register the mock drivers with the shared registry. Idempotent."""
    register("mock_arm", MockArmDriver)
    register("mock_camera", MockCameraDriver)
    register("mock_liquid_handler", MockLiquidHandlerDriver)


# register on import so `import drivers.mock` is enough to make them available
register_mocks()

__all__ = [
    "MockArmDriver", "MockCameraDriver", "MockLiquidHandlerDriver", "register_mocks",
]
