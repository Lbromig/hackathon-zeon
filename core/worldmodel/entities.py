"""The digital twin: a scene graph of entities with poses relative to a parent.

world_pose() composes transforms up to the world frame. Manipulation reparents
entities (pick a tube -> its parent becomes the gripper) while optionally keeping
its world pose fixed. Verification queries this model instead of doing bespoke vision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

Transform = np.ndarray  # 4x4 homogeneous matrix


class EntityKind(str, Enum):
    WORLD = "world"
    CALIB_RULER = "calib_ruler"
    ARM_BASE = "arm_base"
    TCP = "tcp"
    TOOL = "tool"
    CAMERA = "camera"
    OT_BASE = "ot_base"
    DECK = "deck"
    DECK_SLOT = "deck_slot"
    TIP_BOX = "tip_box"
    TIP_SITE = "tip_site"
    TIP = "tip"
    TUBE_RACK = "tube_rack"
    WELL = "well"
    TUBE = "tube"
    CAP = "cap"
    DROPZONE = "dropzone"
    GANTRY = "gantry"
    PIPETTE_CHANNEL = "pipette_channel"
    SURFACE = "surface"


def identity() -> Transform:
    return np.eye(4)


def from_xyz_rpy(x=0.0, y=0.0, z=0.0, roll=0.0, pitch=0.0, yaw=0.0) -> Transform:
    """Pose from translation (m) + intrinsic RPY (rad)."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    R = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = (x, y, z)
    return T


@dataclass
class Entity:
    id: str
    kind: EntityKind
    name: str = ""
    parent: str | None = "world"        # parent entity id (frame parent)
    local: Transform = field(default_factory=identity)  # pose relative to parent
    static: bool = True                 # False = pose updated at runtime
    marker_id: int | None = None        # ArUco id glued to this entity, if any
    dims: dict[str, float] = field(default_factory=dict)   # CAD dimensions (m)
    mesh: str | None = None             # mesh registry key (see worldmodel/meshes.py)
    state: dict[str, Any] = field(default_factory=dict)    # capped?, held_by, has_tip, ...


class WorldModel:
    """Scene graph of entities. Single source of truth for the twin."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.add(Entity("world", EntityKind.WORLD, "World origin", parent=None))

    # --- graph ops ---------------------------------------------------------
    def add(self, e: Entity) -> Entity:
        self.entities[e.id] = e
        return e

    def get(self, eid: str) -> Entity:
        return self.entities[eid]

    def children(self, eid: str) -> list[Entity]:
        return [e for e in self.entities.values() if e.parent == eid]

    def by_kind(self, kind: EntityKind) -> list[Entity]:
        return [e for e in self.entities.values() if e.kind == kind]

    # --- transforms --------------------------------------------------------
    def world_pose(self, eid: str) -> Transform:
        e = self.entities[eid]
        T = e.local
        p = e.parent
        while p is not None:
            parent = self.entities[p]
            T = parent.local @ T
            p = parent.parent
        return T

    def set_world_pose(self, eid: str, world_T: Transform) -> None:
        e = self.entities[eid]
        parent_world = self.world_pose(e.parent) if e.parent else identity()
        e.local = np.linalg.inv(parent_world) @ world_T

    def reparent(self, eid: str, new_parent: str, keep_world_pose: bool = True) -> None:
        """Attach/detach: e.g. tube well->gripper on pick, cap tube->dropzone on remove."""
        e = self.entities[eid]
        if keep_world_pose:
            w = self.world_pose(eid)
            e.parent = new_parent
            self.set_world_pose(eid, w)
        else:
            e.parent = new_parent

    def distance(self, a: str, b: str) -> float:
        return float(np.linalg.norm(self.world_pose(a)[:3, 3] - self.world_pose(b)[:3, 3]))

    # --- persistence -------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for e in self.entities.values():
            w = self.world_pose(e.id)
            out.append({
                "id": e.id, "kind": e.kind, "name": e.name, "parent": e.parent,
                "static": e.static, "marker_id": e.marker_id, "mesh": e.mesh,
                "dims": e.dims, "state": e.state,
                "world_xyz": w[:3, 3].round(4).tolist(),
            })
        return out
