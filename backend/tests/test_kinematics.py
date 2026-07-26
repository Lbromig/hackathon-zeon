"""W3: arm forward-kinematics folds into the twin (no hardware)."""
from __future__ import annotations

import numpy as np

from core.kinematics import pose_to_base_tcp, update_arm_tcp
from core.worldmodel import build_skeleton, from_xyz_rpy
from drivers.capabilities.arm import Pose


def test_pose_to_base_tcp_converts_units():
    T = pose_to_base_tcp(Pose(200.0, -100.0, 300.0, 180.0, 0.0, 90.0))  # mm, deg
    assert np.allclose(T[:3, 3], [0.200, -0.100, 0.300])                 # -> metres


def test_update_arm_tcp_moves_tcp_in_world():
    wm = build_skeleton()
    # place the base somewhere in the world (as calibration would)
    wm.set_world_pose("right_base", from_xyz_rpy(x=0.30, y=0.10, z=0.00))

    pose = Pose(250.0, 0.0, 200.0, 180.0, 0.0, 0.0)                      # base->tcp
    assert update_arm_tcp(wm, "right", pose) is True

    expected = wm.world_pose("right_base") @ pose_to_base_tcp(pose)
    assert np.allclose(wm.world_pose("right_tcp"), expected)
    # +0.25 m along the base's X -> TCP world x = 0.30 + 0.25
    assert np.allclose(wm.world_pose("right_tcp")[:3, 3], [0.55, 0.10, 0.20])


def test_gripper_cam_rides_the_moving_tcp():
    wm = build_skeleton()
    before = wm.world_pose("gripper_cam")[:3, 3].copy()
    update_arm_tcp(wm, "right", Pose(400.0, 50.0, 250.0, 180.0, 0.0, 0.0))
    after = wm.world_pose("gripper_cam")[:3, 3]
    assert not np.allclose(before, after)               # camera on right_tcp moved with it


def test_update_unknown_arm_is_safe():
    wm = build_skeleton()
    assert update_arm_tcp(wm, "nonexistent", Pose(0, 0, 0, 0, 0, 0)) is False
