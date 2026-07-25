"""Tests for core.perception.fiducials (AprilTag tag36h11 detection + pose).

Self-contained: synthesises markers with cv2.aruco.generateImageMarker so it needs
no external images. Requires opencv-contrib-python (cv2.aruco).
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco"):
    pytest.skip("cv2.aruco unavailable (need opencv-contrib-python)", allow_module_level=True)

from core.perception import FiducialDetector, TAG_FAMILY, entity_world_pose, identify_family


def _canvas_with_marker(marker_id: int, side_px: int = 300, pad: int = 60) -> np.ndarray:
    dic = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    tag = cv2.aruco.generateImageMarker(dic, marker_id, side_px)
    canvas = np.full((side_px + 2 * pad, side_px + 2 * pad), 255, np.uint8)
    canvas[pad:pad + side_px, pad:pad + side_px] = tag
    return canvas


def test_detects_and_reads_id():
    img = _canvas_with_marker(224)
    dets = FiducialDetector().detect(img)
    assert [d.marker_id for d in dets] == [224]
    assert dets[0].corners.shape == (4, 2)


def test_entity_resolution_from_marker_map():
    # 224 is mapped to tube_1_cap in core/calibration/markers.py
    det = FiducialDetector().detect(_canvas_with_marker(224))[0]
    assert det.entity_id == "tube_1_cap"


def test_pose_when_intrinsics_given():
    img = _canvas_with_marker(224)
    h, w = img.shape
    f = 1.2 * w
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float)
    det = FiducialDetector(camera_matrix=K).detect(img)[0]
    assert det.T_cam_marker is not None and det.T_cam_marker.shape == (4, 4)
    assert det.distance_m > 0
    # world pose with identity extrinsics equals the cam->marker translation
    Tw = entity_world_pose(det, np.eye(4))
    assert Tw is not None
    assert np.allclose(Tw[:3, 3], det.T_cam_marker[:3, 3])


def test_no_pose_without_intrinsics():
    det = FiducialDetector().detect(_canvas_with_marker(200))[0]
    assert det.T_cam_marker is None and det.distance_m is None


def test_identify_family_flags_apriltag_36h11():
    hits = dict(identify_family(_canvas_with_marker(210)))
    assert any("36h11" in name.lower() for name in hits)
