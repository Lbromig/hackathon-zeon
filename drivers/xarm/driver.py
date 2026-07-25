"""xArm Lite 6 driver — wraps the vendored xArm-Python-SDK.

We run two of these (left / right) cooperatively. Both are calibrated into one
shared world frame; that transform lives in config (``world_from_base``).
"""
from __future__ import annotations

from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.arm import ArmDriver, GripperInfo, GripperKind, Pose

try:  # SDK is optional at import time so the backend can boot without hardware
    from xarm.wrapper import XArmAPI
except Exception:  # pragma: no cover
    XArmAPI = None

DEVICE_TYPE_LITE6 = 9

# Parallel (xArm) gripper opening, controller counts: 0 = closed, 850 = open.
# The counts are 0.1 mm each, so full stroke is 85 mm — override per end effector
# with config["gripper_stroke_m"] if a different one is fitted.
PARALLEL_MIN = 0.0
PARALLEL_MAX = 850.0
PARALLEL_STROKE_M = 0.085
PARALLEL_SPEED = 2000


class XArmDriver(ArmDriver):
    """Config: {"ip": "192.168.1.xxx", "name": "left", "tcp_speed": 120,
    "gripper": "auto"|"parallel"|"lite6"|"bio"|"none", "limits": {...}}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._api: "XArmAPI | None" = None
        self._ip = self.config.get("ip")
        self._gripper_kind: GripperKind = self._configured_gripper()
        self._gripper_ready = False

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.ARM,
            model="xArm Lite 6",
            vendor="UFactory",
            meta={"ip": self._ip, "gripper": self._gripper_kind},
        )

    def connect(self) -> None:
        if XArmAPI is None:
            raise DriverError("xArm SDK not installed (uv sync installs the vendored SDK)")
        if not self._ip:
            raise DriverError("xArm driver requires config['ip']")
        self._state = ConnectionState.CONNECTING
        try:
            self._api = XArmAPI(self._ip, is_radian=False)
            self._api.motion_enable(True)
            self._api.set_mode(0)
            self._api.set_state(0)
            self._gripper_kind = self._resolve_gripper(self._api)
            self._gripper_ready = False
            self._state = ConnectionState.CONNECTED
        except Exception as e:  # pragma: no cover
            self._state = ConnectionState.ERROR
            raise DriverError(f"xArm connect failed: {e}") from e

    def disconnect(self) -> None:
        if self._api is not None:
            self._api.disconnect()
        self._api = None
        self._gripper_ready = False
        self._gripper_kind = self._configured_gripper()
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        if self._api is None:
            return {"state": self._state, "connected": False}
        out: dict[str, Any] = {
            "state": self._state,
            "connected": True,
            "pose": self.get_pose().__dict__,
            "mode": self._api.mode,
            "arm_state": self._api.state,
            "error_code": self._api.error_code,
            "warn_code": self._api.warn_code,
        }
        try:
            # Guarded on its own: a joint read failing must not blank the rest of
            # the snapshot (`_safe_status` would replace the whole dict).
            out["joints"] = self.get_joints()
        except Exception:
            pass
        return out

    # --- motion ------------------------------------------------------------
    def _require(self) -> "XArmAPI":
        if self._api is None:
            raise DriverError("xArm not connected")
        return self._api

    @staticmethod
    def _check(code: int, what: str) -> None:
        if code != 0:
            raise DriverError(f"{what} failed (code={code})")

    def enable(self, on: bool = True) -> None:
        api = self._require()
        api.motion_enable(on)
        api.set_state(0 if on else 4)

    def home(self) -> None:
        self._check(self._require().move_gohome(wait=True), "move_gohome")

    def stop(self, emergency: bool = False) -> None:
        api = self._require()
        if emergency:
            api.emergency_stop()
        else:
            api.set_state(4)  # 4 = stop

    def clear_errors(self) -> None:
        """Clear latched faults and bring the arm back to a movable state.

        Mirrors the bring-up sequence in scripts/init_xarm.py: an emergency stop
        or a collision latches an error that blocks every subsequent motion
        command until it's cleaned *and* the arm is re-enabled.
        """
        api = self._require()
        api.clean_warn()
        api.clean_error()
        self._check(api.motion_enable(True), "motion_enable")
        self._check(api.set_mode(0), "set_mode(0)")
        self._check(api.set_state(0), "set_state(0)")

    def get_pose(self) -> Pose:
        code, p = self._require().get_position(is_radian=False)
        if code != 0:
            raise DriverError(f"get_position error {code}")
        return Pose(*p)  # [x, y, z, roll, pitch, yaw]

    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None:
        self._check(self._require().set_position(
            x=pose.x, y=pose.y, z=pose.z,
            roll=pose.roll, pitch=pose.pitch, yaw=pose.yaw,
            speed=speed or self.config.get("tcp_speed", 100), wait=wait,
        ), "set_position")

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0,
                      speed: float | None = None, wait=True) -> None:
        self._check(self._require().set_position(
            x=dx, y=dy, z=dz, roll=droll, pitch=dpitch, yaw=dyaw,
            relative=True, wait=wait, speed=speed or self.config.get("tcp_speed", 100),
        ), "set_position(relative)")

    # --- joint space ---------------------------------------------------------
    def get_joints(self) -> list[float]:
        code, angles = self._require().get_servo_angle(is_radian=False)
        if code != 0:
            raise DriverError(f"get_servo_angle error {code}")
        return [float(a) for a in angles[: self.axis_count]]

    def move_joints(self, angles: list[float], speed: float | None = None,
                    wait: bool = True) -> None:
        self._check(self._require().set_servo_angle(
            angle=list(angles), speed=speed or self.config.get("joint_speed", 20),
            is_radian=False, wait=wait,
        ), "set_servo_angle")

    def move_joints_relative(self, deltas: list[float], speed: float | None = None,
                             wait: bool = True) -> None:
        self._check(self._require().set_servo_angle(
            angle=list(deltas), speed=speed or self.config.get("joint_speed", 20),
            relative=True, is_radian=False, wait=wait,
        ), "set_servo_angle(relative)")

    def check_joint_target(self, angles: list[float]) -> str | None:
        """Soft-limit check, plus the controller's own verdict when connected."""
        reason = super().check_joint_target(angles)
        if reason or self._api is None:
            return reason
        try:
            if self._api.is_joint_limit(list(angles)):
                return "target is outside the controller's joint limits"
        except Exception:
            pass  # advisory only — the controller rejects it anyway
        return None

    @property
    def axis_count(self) -> int:
        if self._api is not None:
            try:
                return int(self._api.axis)
            except Exception:
                pass
        return super().axis_count

    # --- gripper -------------------------------------------------------------
    def _configured_gripper(self) -> GripperKind:
        kind = str(self.config.get("gripper", "auto")).lower()
        return "unknown" if kind == "auto" else kind  # type: ignore[return-value]

    def _resolve_gripper(self, api: "XArmAPI") -> GripperKind:
        """`auto` picks the end-effector API matching the connected model.

        Same rule as scripts/init_xarm.py — a Lite 6 carries the pneumatic
        gripper, everything else the parallel one.
        """
        configured = self._configured_gripper()
        if configured != "unknown":
            return configured
        try:
            return "lite6" if api.device_type == DEVICE_TYPE_LITE6 else "parallel"
        except Exception:
            return "unknown"

    @property
    def gripper_info(self) -> GripperInfo:
        parallel = self._gripper_kind == "parallel"
        return GripperInfo(
            kind=self._gripper_kind,
            supports_width=parallel,
            min_width=PARALLEL_MIN if parallel else 0.0,
            max_width=PARALLEL_MAX if parallel else 0.0,
            units="counts",
            stroke_m=float(self.config.get("gripper_stroke_m", PARALLEL_STROKE_M)) if parallel else 0.0,
        )

    def _prepare_gripper(self) -> "XArmAPI":
        """Enable the end effector once per connection."""
        api = self._require()
        if self._gripper_kind in ("none", "unknown"):
            raise DriverError(f"no gripper configured for {self.device_id}")
        if not self._gripper_ready:
            if self._gripper_kind == "parallel":
                self._check(api.set_gripper_mode(0), "set_gripper_mode")
                self._check(api.set_gripper_enable(True), "set_gripper_enable")
                self._check(api.set_gripper_speed(PARALLEL_SPEED), "set_gripper_speed")
            elif self._gripper_kind == "bio":
                self._check(api.set_bio_gripper_enable(True), "set_bio_gripper_enable")
            self._gripper_ready = True
        return api

    def grip(self, width: float | None = None, force: float | None = None) -> None:
        """Close the gripper. ``width`` is in controller counts (see gripper_info.units).

        An out-of-range width is rejected rather than clamped: a caller that hands
        us metres (0.011) would otherwise silently get "fully closed" and crush
        whatever is in the jaws. Convert with ``width_from_metres()`` first.
        """
        api = self._prepare_gripper()
        if self._gripper_kind == "parallel":
            if width is None:
                pos = PARALLEL_MIN
            elif not PARALLEL_MIN <= width <= PARALLEL_MAX:
                raise DriverError(
                    f"gripper width {width!r} is outside {PARALLEL_MIN:g}..{PARALLEL_MAX:g} counts "
                    f"— pass counts, not metres (use width_from_metres())"
                )
            else:
                pos = float(width)
            self._check(api.set_gripper_position(pos, wait=True), "set_gripper_position")
        elif self._gripper_kind == "lite6":
            api.close_lite6_gripper()
        elif self._gripper_kind == "bio":
            self._check(api.close_bio_gripper(wait=True), "close_bio_gripper")

    def release(self) -> None:
        api = self._prepare_gripper()
        if self._gripper_kind == "parallel":
            self._check(api.set_gripper_position(PARALLEL_MAX, wait=True), "set_gripper_position")
        elif self._gripper_kind == "lite6":
            api.open_lite6_gripper()
        elif self._gripper_kind == "bio":
            self._check(api.open_bio_gripper(wait=True), "open_bio_gripper")

    def gripper_width(self) -> float | None:
        """Opening in controller units, or None when the gripper has no feedback."""
        if self._gripper_kind != "parallel":
            return None
        code, pos = self._require().get_gripper_position()
        if code != 0:
            raise DriverError(f"get_gripper_position error {code}")
        return float(pos)
