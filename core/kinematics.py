"""Arm forward-kinematics → twin (W3).

Each arm reports its live TCP pose relative to its own base (mm + degrees). The twin models
`{arm}_base` (world-fixed, set by calibration) → `{arm}_tcp` (child of base) → `{arm}_tool`
(fixed tool offset), and the on-arm `gripper_cam` rides `right_tcp`. So writing the arm's
reported pose straight into `{arm}_tcp.local` moves the whole chain — TCP, tool and gripper
camera — in the twin every tick. This is what makes the twin's moving parts actually move
(and lets `tube_aligned` and the world map reflect where the arm really is).

Pure + hardware-free: `pose_to_base_tcp` and `update_arm_tcp` operate on a Pose-like object
and a WorldModel, so they unit-test with the mock arm. The backend loop lives in
backend/app/services/kinematics.py.

Note: the xArm Euler convention (roll/pitch/yaw about x/y/z) is mapped through
`from_xyz_rpy`; position is exact, and orientation is good enough for the twin/overlay.
"""
from __future__ import annotations

import numpy as np

from .worldmodel import WorldModel
from .worldmodel.entities import Transform, from_xyz_rpy


def pose_to_base_tcp(pose) -> Transform:
    """xArm Pose (mm, degrees; base→TCP) → 4x4 transform in metres/radians."""
    return from_xyz_rpy(
        x=pose.x / 1000.0, y=pose.y / 1000.0, z=pose.z / 1000.0,
        roll=np.radians(pose.roll), pitch=np.radians(pose.pitch), yaw=np.radians(pose.yaw),
    )


def update_arm_tcp(wm: WorldModel, arm_id: str, pose) -> bool:
    """Write the arm's live TCP into the twin as `{arm}_tcp.local` (relative to its base).

    Returns False if the twin has no such TCP entity (e.g. before calibration built it).
    """
    tcp = f"{arm_id}_tcp"
    with wm.lock:
        if tcp not in wm.entities:
            return False
        wm.get(tcp).local = pose_to_base_tcp(pose)
    return True
