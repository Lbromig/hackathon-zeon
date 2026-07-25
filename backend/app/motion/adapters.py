"""Movers bind the execution-agnostic planner to a concrete backend.

- `ArmDriverMover` drives our own drivers.ArmDriver (converts m->mm, rad->deg).
- The ZEON equivalent is a ~10-line wrapper over move_arm / set_gripper /
  attach_object_to_arm (see docs/ZEON_INTEGRATION.md).
"""
from __future__ import annotations

import math
from typing import Callable

from drivers import ArmDriver, Pose


class ArmDriverMover:
    """Adapt drivers.ArmDriver to the planner's Mover protocol.

    Planner speaks metres + radians; our ArmDriver.Pose is mm + degrees, so convert.
    attach/detach are delegated to callbacks (they update the digital twin, not the arm).
    """

    def __init__(self, arm: ArmDriver, *,
                 on_attach: Callable[[str], None] | None = None,
                 on_detach: Callable[[str], None] | None = None) -> None:
        self.arm = arm
        self._on_attach = on_attach
        self._on_detach = on_detach

    def move(self, xyz, rpy, speed) -> None:
        self.arm.move_to(Pose(
            x=xyz[0] * 1000, y=xyz[1] * 1000, z=xyz[2] * 1000,
            roll=math.degrees(rpy[0]), pitch=math.degrees(rpy[1]), yaw=math.degrees(rpy[2]),
        ), speed=speed)

    def gripper(self, width) -> None:
        # width in metres; ArmDriver.grip expects its own units — adapt as needed per gripper
        self.arm.grip(width=width)

    def attach(self, object_id: str) -> None:
        if self._on_attach:
            self._on_attach(object_id)

    def detach(self, object_id: str) -> None:
        if self._on_detach:
            self._on_detach(object_id)
