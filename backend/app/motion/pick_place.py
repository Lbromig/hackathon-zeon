"""Safe tube pick & place — waypoint planner + upright (anti-spill) guard.

Encodes the capability spec in docs/CAPABILITY_pick_place.md:
  current -> SAFE_TRANSIT_Z -> APPROACH_ABOVE -> FINAL_APPROACH (grasp/place) -> retreat.

Units are metres + radians (world frame), matching the ZEON runtime and our worldmodel.
The planner is execution-agnostic: give it a `Mover` (our ArmDriver, or ZEON's move_arm).
The guard refuses any waypoint whose orientation departs from the grasp orientation by more
than MAX_TILT — so a grasped tube can never be flipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import numpy as np

from ..worldmodel.entities import from_xyz_rpy


class UprightViolation(RuntimeError):
    """A commanded pose would tilt/flip the tube past MAX_TILT (SI1/SI2)."""


@dataclass
class PickPlaceConfig:
    safe_transit_z: float                 # user-defined medium transit height (m), world Z
    approach_standoff: float = 0.05       # APPROACH_ABOVE height above target (m)
    grasp_width: float = 0.011            # gripper opening to grasp the tube (m)
    open_width: float = 0.03              # gripper opening to release/clear (m)
    max_tilt_deg: float = 20.0            # max tube tilt from grasp orientation while held
    place_pos_tol: float = 0.002          # 2 mm
    place_rot_tol_deg: float = 3.0
    speed_transit: float = 100.0
    speed_fine: float = 30.0


class StepKind(str, Enum):
    MOVE = "move"
    GRIP_CLOSE = "grip_close"
    GRIP_OPEN = "grip_open"
    ATTACH = "attach"
    DETACH = "detach"


@dataclass
class Waypoint:
    name: str
    xyz: list[float]
    rpy: list[float]
    fine: bool = False           # True = final-approach speed, pure vertical segment


@dataclass
class Step:
    kind: StepKind
    waypoint: Waypoint | None = None


class Mover(Protocol):
    """Minimal execution surface. Adapt to ArmDriver or ZEON move_arm/set_gripper."""
    def move(self, xyz: list[float], rpy: list[float], speed: float) -> None: ...
    def gripper(self, width: float) -> None: ...
    def attach(self, object_id: str) -> None: ...
    def detach(self, object_id: str) -> None: ...


def _rot(rpy) -> np.ndarray:
    return from_xyz_rpy(0, 0, 0, *rpy)[:3, :3]


def rotation_angle(rpy_a, rpy_b) -> float:
    """Absolute rotation angle (rad) between two orientations."""
    R = _rot(rpy_a).T @ _rot(rpy_b)
    return float(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0)))


def assert_upright(rpy, grasp_rpy, max_tilt_deg: float) -> None:
    if rotation_angle(rpy, grasp_rpy) > np.deg2rad(max_tilt_deg):
        raise UprightViolation(
            f"orientation {np.round(rpy,3).tolist()} exceeds max tilt "
            f"{max_tilt_deg}° from grasp orientation — refusing (anti-spill)"
        )


def plan(pick_xyz, place_xyz, grasp_rpy, cfg: PickPlaceConfig,
         object_id: str = "tube") -> list[Step]:
    """Build the full pick->place program. Orientation is held at grasp_rpy throughout
    (SI2), so the tube stays upright. All lateral travel is at safe_transit_z (FR4);
    final approach/retreat are pure vertical (FR5)."""
    sz, so = cfg.safe_transit_z, cfg.approach_standoff
    px, py, pz = pick_xyz
    dx, dy, dz = place_xyz
    r = list(grasp_rpy)

    def wp(name, xyz, fine=False) -> Waypoint:
        return Waypoint(name, [round(v, 4) for v in xyz], r, fine)

    program = [
        # --- PICK ---
        Step(StepKind.MOVE, wp("safe_over_pick", [px, py, sz])),
        Step(StepKind.MOVE, wp("approach_above_pick", [px, py, pz + so], fine=True)),
        Step(StepKind.MOVE, wp("grasp", [px, py, pz], fine=True)),
        Step(StepKind.GRIP_CLOSE),
        Step(StepKind.ATTACH),
        Step(StepKind.MOVE, wp("retreat_pick", [px, py, pz + so], fine=True)),
        Step(StepKind.MOVE, wp("safe_after_pick", [px, py, sz])),
        # --- TRANSIT (lateral only at safe height) ---
        Step(StepKind.MOVE, wp("safe_over_place", [dx, dy, sz])),
        # --- PLACE ---
        Step(StepKind.MOVE, wp("approach_above_place", [dx, dy, dz + so], fine=True)),
        Step(StepKind.MOVE, wp("place", [dx, dy, dz], fine=True)),
        Step(StepKind.GRIP_OPEN),
        Step(StepKind.DETACH),
        Step(StepKind.MOVE, wp("retreat_place", [dx, dy, dz + so], fine=True)),
        Step(StepKind.MOVE, wp("safe_after_place", [dx, dy, sz])),
    ]
    # pre-flight: every held-motion waypoint must satisfy the upright invariant
    for s in program:
        if s.kind is StepKind.MOVE:
            assert_upright(s.waypoint.rpy, grasp_rpy, cfg.max_tilt_deg)
    return program


def execute(program: list[Step], mover: Mover, cfg: PickPlaceConfig,
            object_id: str = "tube") -> None:
    """Run the program, re-checking the upright guard before every commanded move.
    On any violation, abort to a vertical retreat (SI4) instead of continuing."""
    held = False
    grasp_rpy = next(s.waypoint.rpy for s in program if s.kind is StepKind.MOVE)
    for s in program:
        if s.kind is StepKind.MOVE:
            w = s.waypoint
            if held:  # only enforce while carrying the tube
                try:
                    assert_upright(w.rpy, grasp_rpy, cfg.max_tilt_deg)
                except UprightViolation:
                    _abort_to_safe(mover, w, cfg)
                    raise
            mover.move(w.xyz, w.rpy, cfg.speed_fine if w.fine else cfg.speed_transit)
        elif s.kind is StepKind.GRIP_CLOSE:
            mover.gripper(cfg.grasp_width)
        elif s.kind is StepKind.GRIP_OPEN:
            mover.gripper(cfg.open_width)
        elif s.kind is StepKind.ATTACH:
            mover.attach(object_id); held = True
        elif s.kind is StepKind.DETACH:
            mover.detach(object_id); held = False


def _abort_to_safe(mover: Mover, current: Waypoint, cfg: PickPlaceConfig) -> None:
    """SI4: retreat straight up to safe height, keeping the grasp orientation."""
    x, y, _ = current.xyz
    mover.move([x, y, cfg.safe_transit_z], current.rpy, cfg.speed_fine)
