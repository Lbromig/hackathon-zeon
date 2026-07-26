"""Capability interface for 6-axis robot arms.

Two motion spaces are exposed: cartesian (``Pose``, mm + deg) and joint space
(a list of joint angles in deg). Teach/jog tooling needs both — cartesian for
"move 1 mm in Z", joint space for replaying a physically taught point without
IK ambiguity.
"""
from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from ..base import InstrumentDriver, InstrumentKind

# Which end-effector API a given arm speaks. Only ``parallel`` has a
# commandable opening width; the others are binary open/close. ``unknown`` means
# the arm is configured for auto-detection but isn't connected yet.
GripperKind = Literal["parallel", "lite6", "bio", "none", "unknown"]


@dataclass
class Pose:
    """Cartesian TCP pose in the shared world frame. Position mm, orientation deg."""
    x: float
    y: float
    z: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0


@dataclass
class GripperInfo:
    """Describes a gripper's commandable opening.

    ``min_width``/``max_width`` are in ``units`` — for the xArm parallel gripper
    those are *controller counts*, not metres. Callers working in SI (the motion
    planner, the world model) MUST convert via ``ArmDriver.width_from_metres()``;
    passing metres straight through reads as "fully closed" and crushes the payload.
    """
    kind: GripperKind = "none"
    supports_width: bool = False
    min_width: float = 0.0
    max_width: float = 0.0
    units: str = "counts"            # "counts" | "m"
    stroke_m: float = 0.0            # physical opening at max_width, metres


@dataclass
class ArmLimits:
    """Soft limits enforced *above* the driver — the guard for hand-driven motion.

    These do not replace the controller's own limit enforcement; they bound what
    a UI or script is allowed to ask for in one command.

    ``joints`` is left ``None`` unless configured: publishing a guessed joint
    range would imply a safety guarantee we don't have. Fill it in from the
    model's manual via the driver config to get UI limit rails + pre-flight
    range checks.
    """
    joints: list[tuple[float, float]] | None = None
    max_jog_linear: float = 50.0        # mm, per jog command
    max_jog_angular: float = 15.0       # deg, per jog command
    max_speed_linear: float = 200.0     # mm/s
    max_speed_angular: float = 60.0     # deg/s
    max_move_to_jump: float = 250.0     # mm, cartesian distance for one absolute move

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "ArmLimits":
        cfg = dict(cfg or {})
        joints = cfg.pop("joints", None)
        limits = cls(**{k: float(v) for k, v in cfg.items()})
        if joints:
            limits.joints = [(float(lo), float(hi)) for lo, hi in joints]
        return limits


class ArmDriver(InstrumentDriver):
    kind = InstrumentKind.ARM

    # --- introspection -----------------------------------------------------
    @property
    def axis_count(self) -> int:
        return int(self.config.get("axis_count", 6))

    @property
    def limits(self) -> ArmLimits:
        """Soft limits from ``config["limits"]``; defaults otherwise."""
        cached = getattr(self, "_limits", None)
        if cached is None:
            cached = ArmLimits.from_config(self.config.get("limits"))
            self._limits = cached
        return cached

    @property
    def gripper_info(self) -> GripperInfo:
        return GripperInfo()

    # --- lifecycle / faults ------------------------------------------------
    @abstractmethod
    def enable(self, on: bool = True) -> None: ...

    @abstractmethod
    def home(self) -> None: ...

    @abstractmethod
    def stop(self, emergency: bool = False) -> None:
        """Halt motion. ``emergency`` uses the hardest stop the arm offers."""

    @abstractmethod
    def clear_errors(self) -> None:
        """Clear latched faults/warnings and return the arm to a movable state."""

    def set_free_drive(self, on: bool = True) -> None:
        """Enter or leave hand-guiding mode, where the arm can be pushed by hand.

        Not every arm has one. Implementations must guarantee that programmed motion
        is impossible while it is active, and callers must always turn it back off —
        commanded moves do not behave normally in a teaching mode.
        """
        raise NotImplementedError(f"{type(self).__name__} has no free-drive mode")

    # --- cartesian ---------------------------------------------------------
    @abstractmethod
    def get_pose(self) -> Pose: ...

    @abstractmethod
    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None: ...

    @abstractmethod
    def move_relative(self, dx: float = 0, dy: float = 0, dz: float = 0,
                      droll: float = 0, dpitch: float = 0, dyaw: float = 0,
                      speed: float | None = None, wait: bool = True) -> None: ...

    # --- joint space -------------------------------------------------------
    @abstractmethod
    def get_joints(self) -> list[float]:
        """Joint angles in deg, one per axis."""

    @abstractmethod
    def move_joints(self, angles: list[float], speed: float | None = None,
                    wait: bool = True) -> None:
        """Absolute joint move. ``angles`` in deg, one per axis."""

    @abstractmethod
    def move_joints_relative(self, deltas: list[float], speed: float | None = None,
                             wait: bool = True) -> None:
        """Relative joint move. ``deltas`` in deg, one per axis."""

    def check_pose_target(self, pose: Pose) -> str | None:
        """Pre-flight a cartesian target. Returns a reason string, or None if fine.

        Base implementation has no kinematic model, so it can only pass. Drivers that
        can solve IK should check the resulting joint angles against the soft limits —
        otherwise cartesian moves silently bypass every joint limit.
        """
        return None

    def check_joint_target(self, angles: list[float]) -> str | None:
        """Pre-flight a joint target. Returns a reason string, or None if it's fine.

        Base implementation checks the configured soft limits; drivers can
        additionally ask the controller.
        """
        joint_limits = self.limits.joints
        if not joint_limits:
            return None
        for i, angle in enumerate(angles[: len(joint_limits)]):
            lo, hi = joint_limits[i]
            if not lo <= angle <= hi:
                return f"J{i + 1}={angle:.2f}° outside soft limit [{lo:g}, {hi:g}]"
        return None

    # --- gripper -----------------------------------------------------------
    def width_from_metres(self, width_m: float) -> float:
        """Convert an SI opening (metres) into this gripper's command units.

        The planner and world model are metric; grippers are not. Anything that
        computes a width from geometry must route through here.
        """
        info = self.gripper_info
        if not info.supports_width:
            raise ValueError(f"the {info.kind} gripper has no commandable width")
        if not math.isfinite(width_m):
            raise ValueError(f"width {width_m!r} is not a finite number")
        if info.units == "m":
            return width_m
        if info.stroke_m <= 0:
            raise ValueError(f"{info.kind} gripper has no stroke calibration")
        if not 0.0 <= width_m <= info.stroke_m:
            raise ValueError(
                f"width {width_m * 1000:.1f} mm is outside the gripper's "
                f"0..{info.stroke_m * 1000:.1f} mm stroke"
            )
        span = info.max_width - info.min_width
        return info.min_width + (width_m / info.stroke_m) * span

    @abstractmethod
    def grip(self, width: float | None = None, force: float | None = None) -> None:
        """Close the gripper, to ``width`` in ``gripper_info.units`` if supported.

        Implementations must REJECT an out-of-range width rather than clamp it:
        clamping silently converts a unit-conversion bug into a crushed payload.
        """

    @abstractmethod
    def release(self) -> None:
        """Open the gripper fully."""

    @abstractmethod
    def gripper_width(self) -> float | None:
        """Current opening, or None for grippers with no width feedback."""
