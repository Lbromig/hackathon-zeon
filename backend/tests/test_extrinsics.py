"""W2: fixed-camera extrinsics from the 210/211 board (no hardware)."""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.calibration.extrinsics import calibrate_fixed_camera, solve_world_cam
from core.calibration.world_board import board_object_points
from core.perception import TwinFuser
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton
from core.worldmodel.entities import from_xyz_rpy


def _K(w=1280, h=720, f=900.0):
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float)


def _project(T_cam_world, ids, K):
    """Project the given board tags through a known camera pose -> {id: (4,2) px}."""
    rvec, _ = cv2.Rodrigues(T_cam_world[:3, :3])
    tvec = T_cam_world[:3, 3].reshape(3, 1)
    objp = board_object_points()
    allp = np.vstack([objp[i] for i in ids])
    img, _ = cv2.projectPoints(allp, rvec, tvec, K, None)
    img = img.reshape(-1, 2)
    return {tid: img[4 * k:4 * k + 4] for k, tid in enumerate(ids)}


def test_solve_world_cam_roundtrip_two_tags():
    K = _K()
    T_cam_world = from_xyz_rpy(x=0.05, y=-0.03, z=0.5,
                               roll=np.radians(8), pitch=np.radians(-5))
    corners = _project(T_cam_world, sorted(board_object_points()), K)
    T_world_cam = solve_world_cam(corners, K)
    assert T_world_cam is not None
    assert np.allclose(T_world_cam, np.linalg.inv(T_cam_world), atol=1e-3)


def test_single_board_tag_still_solves():
    K = _K()
    T_cam_world = from_xyz_rpy(z=0.4)
    one = sorted(board_object_points())[0]
    corners = _project(T_cam_world, [one], K)
    T_world_cam = solve_world_cam(corners, K)
    assert T_world_cam is not None
    assert np.allclose(T_world_cam, np.linalg.inv(T_cam_world), atol=1e-2)


class _FakeDetector:
    """Returns detections with .marker_id + .corners, like FiducialDetector.detect."""
    def __init__(self, corners_by_id):
        self._c = corners_by_id

    def detect(self, _frame):
        return [type("D", (), {"marker_id": m, "corners": c}) for m, c in self._c.items()]


def test_calibrate_fixed_camera_writes_twin_and_filters_non_board():
    K = _K()
    T_cam_world = from_xyz_rpy(x=0.1, z=0.6, yaw=np.radians(15))
    ids = sorted(board_object_points())
    corners = _project(T_cam_world, ids, K)
    corners[224] = corners[ids[0]]                      # distractor, must be ignored

    wm = build_skeleton()
    r = calibrate_fixed_camera(wm, "overview_cam", frame=None, K=K,
                               detector=_FakeDetector(corners))
    assert r.ok and r.n_board_tags == 2                 # 224 filtered by WORLD_BOARD_IDS
    assert np.allclose(wm.world_pose("overview_cam"), np.linalg.inv(T_cam_world), atol=1e-3)


def test_fusion_is_world_correct_after_extrinsics():
    """The point of W2: once the camera pose is set, fusion yields real world coords."""
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")

    T_world_cam = from_xyz_rpy(x=0.2, y=0.1, z=0.5, yaw=np.radians(20))
    wm.set_world_pose("overview_cam", T_world_cam)      # as calibration would

    p_cam = (0.0, 0.0, 0.3)                              # 0.3 m in front of the camera
    r = TwinFuser().fuse_point(wm, "overview_cam", "tube_1_cap", p_cam)
    expected = (T_world_cam @ np.array([0, 0, 0.3, 1.0]))[:3]
    assert r.ok
    assert np.allclose(wm.world_pose("tube_1_cap")[:3, 3], expected, atol=1e-6)
