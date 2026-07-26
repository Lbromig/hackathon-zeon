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

from core.perception import (DEFAULT_TAG_SIZE_M, FiducialDetector, TAG_FAMILY,
                             identify_family, invert, spec_for)
from core.perception.markers import BENCH_TAG_IDS


def _canvas_with_marker(marker_id: int, side_px: int = 300, pad: int = 60) -> np.ndarray:
    dic = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    tag = cv2.aruco.generateImageMarker(dic, marker_id, side_px)
    canvas = np.full((side_px + 2 * pad, side_px + 2 * pad), 255, np.uint8)
    canvas[pad:pad + side_px, pad:pad + side_px] = tag
    return canvas


def test_detects_and_reads_id():
    img = _canvas_with_marker(225)
    dets = FiducialDetector().detect(img)
    assert [d.marker_id for d in dets] == [225]
    assert dets[0].corners.shape == (4, 2)


def test_size_comes_from_the_bench_registry_and_falls_back_otherwise():
    """The registry's only job is the PnP object scale (core/perception/markers.py).

    225 is the tag observed on the tube/gripper assembly (P-5); 200 was never on the
    bench, and an unregistered id must still detect — just at the stock default size,
    because refusing it would hide a real tag from the overlay.
    """
    assert 225 in BENCH_TAG_IDS and spec_for(225).size_m == DEFAULT_TAG_SIZE_M
    assert spec_for(200) is None
    known = FiducialDetector().detect(_canvas_with_marker(225))[0]
    unknown = FiducialDetector().detect(_canvas_with_marker(200))[0]
    assert known.size_m == DEFAULT_TAG_SIZE_M and unknown.size_m == DEFAULT_TAG_SIZE_M


def test_pose_when_intrinsics_given():
    img = _canvas_with_marker(225)
    h, w = img.shape
    f = 1.2 * w
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float)
    det = FiducialDetector(camera_matrix=K).detect(img)[0]
    assert det.T_cam_marker is not None and det.T_cam_marker.shape == (4, 4)
    assert det.distance_m > 0
    # The salvaged geometry helper must round-trip the pose it is handed.
    assert np.allclose(invert(det.T_cam_marker) @ det.T_cam_marker, np.eye(4), atol=1e-9)


def test_no_pose_without_intrinsics():
    det = FiducialDetector().detect(_canvas_with_marker(200))[0]
    assert det.T_cam_marker is None and det.distance_m is None


def test_identify_family_flags_apriltag_36h11():
    hits = dict(identify_family(_canvas_with_marker(210)))
    assert any("36h11" in name.lower() for name in hits)
