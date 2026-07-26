"""Fuse camera detections into the digital twin — the perception -> twin loop (FR-WM).

A detection gives a tag/object centre in the *camera* frame (metres). The camera's own
pose in the world frame is read straight from the twin (`world_pose(cam_entity_id)`) — for
the fixed cameras that pose comes from calibration, for the gripper cam from arm kinematics.
Compose the two to get the object's world position and write it back as a *corrective*
update to that entity's pose.

Position-only by design: a 20 mm AprilTag subtends few pixels so its solved orientation is
noisy (and OpenCV's AprilTag corner order makes yaw ambiguous), while depth gives a solid
metric position. So we update each entity's world *translation* and keep its existing
orientation (from kinematics / prior). Gating: drop low-confidence detections and reject
implausible jumps once an entity has been seen, so a bad frame never yanks the twin.

Pure and hardware-free: everything operates on a WorldModel + plain points, so it unit-tests
without cameras. The backend wiring lives in backend/app/services/twin_fusion.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..worldmodel import WorldModel


@dataclass
class FuseResult:
    entity_id: str
    ok: bool
    reason: str
    world_xyz: list[float] | None = None
    jump_m: float | None = None


class TwinFuser:
    """Stateful fuser: remembers the last update per entity for the jump gate."""

    def __init__(self, *, min_confidence: float = 0.3, max_jump_m: float = 0.25) -> None:
        self.min_confidence = min_confidence
        self.max_jump_m = max_jump_m
        self._seen: dict[str, float] = {}   # entity_id -> last update stamp

    def fuse_point(
        self,
        wm: WorldModel,
        cam_entity_id: str,
        entity_id: str,
        point_cam,
        *,
        confidence: float = 1.0,
        stamp: float | None = None,
        offset_world=(0.0, 0.0, 0.0),
    ) -> FuseResult:
        """Update `entity_id`'s world position from a camera-frame point.

        point_cam : (x, y, z) of the observed centre in the camera's frame, metres.
        offset_world : optional marker-centre -> entity-origin translation (world axes).
        """
        stamp = time.monotonic() if stamp is None else stamp
        if entity_id not in wm.entities:
            return FuseResult(entity_id, False, "unknown entity")
        if cam_entity_id not in wm.entities:
            return FuseResult(entity_id, False, f"unknown camera {cam_entity_id}")
        if point_cam is None:
            return FuseResult(entity_id, False, "no point")
        if confidence < self.min_confidence:
            return FuseResult(entity_id, False, f"low confidence {confidence:.2f}")

        T_world_cam = wm.world_pose(cam_entity_id)
        p_cam = np.array([float(point_cam[0]), float(point_cam[1]), float(point_cam[2]), 1.0])
        p_world = (T_world_cam @ p_cam)[:3] + np.asarray(offset_world, float)

        cur = wm.world_pose(entity_id)[:3, 3]
        jump = float(np.linalg.norm(p_world - cur))
        if entity_id in self._seen and jump > self.max_jump_m:
            return FuseResult(entity_id, False, f"jump {jump*1000:.0f}mm rejected", jump_m=jump)

        target = wm.world_pose(entity_id).copy()   # keep orientation, replace translation
        target[:3, 3] = p_world
        wm.set_world_pose(entity_id, target)
        self._seen[entity_id] = stamp
        return FuseResult(entity_id, True, "ok",
                          [round(float(x), 4) for x in p_world], jump_m=jump)

    def reset(self, entity_id: str | None = None) -> None:
        """Forget jump-gate history (e.g. after a deliberate reparent/teleport)."""
        if entity_id is None:
            self._seen.clear()
        else:
            self._seen.pop(entity_id, None)
