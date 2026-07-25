"""Capability interface for 6-axis robot arms."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from ..base import InstrumentDriver, InstrumentKind


@dataclass
class Pose:
    """Cartesian TCP pose in the shared world frame. Position mm, orientation deg."""
    x: float
    y: float
    z: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0


class ArmDriver(InstrumentDriver):
    kind = InstrumentKind.ARM

    @abstractmethod
    def enable(self, on: bool = True) -> None: ...

    @abstractmethod
    def home(self) -> None: ...

    @abstractmethod
    def get_pose(self) -> Pose: ...

    @abstractmethod
    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None: ...

    @abstractmethod
    def move_relative(self, dx: float = 0, dy: float = 0, dz: float = 0,
                      droll: float = 0, dpitch: float = 0, dyaw: float = 0,
                      wait: bool = True) -> None: ...

    @abstractmethod
    def grip(self, width: float | None = None, force: float | None = None) -> None: ...

    @abstractmethod
    def release(self) -> None: ...

    @abstractmethod
    def gripper_width(self) -> float: ...
