"""Tests for core.perception.shapes (classical-CV circle detection)."""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.perception import ShapeDetector


def _image_with_circle(cx=320, cy=240, r=40, w=640, h=480):
    img = np.full((h, w, 3), 230, np.uint8)          # light background
    cv2.circle(img, (cx, cy), r, (40, 40, 40), -1)   # dark filled disc
    return img


def test_finds_a_circle_near_center():
    det = ShapeDetector(param2=20)                    # permissive for the clean synthetic
    shapes = det.detect(_image_with_circle())
    assert shapes, "expected at least one circle"
    s = max(shapes, key=lambda s: s.radius_px)
    assert abs(s.center[0] - 0.5) < 0.05 and abs(s.center[1] - 0.5) < 0.05
    assert 30 < s.radius_px < 55
    assert s.source == "cv"
    assert s.camera_xyz is None and s.kind == "circle"   # no intrinsics -> no metric


def test_back_projects_with_depth_and_intrinsics():
    fx = 600.0
    intr = {"fx": fx, "fy": fx, "cx": 320, "cy": 240, "width": 640, "height": 480}
    det = ShapeDetector(intr, param2=20)
    depth = np.full((480, 640), 0.5, np.float32)      # 0.5 m everywhere
    shapes = det.detect(_image_with_circle(r=40), depth)
    s = max(shapes, key=lambda s: s.radius_px)
    assert s.camera_xyz is not None
    assert abs(s.camera_xyz[2] - 0.5) < 1e-6           # z == sampled depth
    assert abs(s.diameter_m - (2 * s.radius_px * 0.5 / fx)) < 1e-6   # formula consistent
    assert abs(s.diameter_m - (2 * 40 * 0.5 / fx)) < 0.01           # ~ the drawn Ø
    assert s.kind in {"cap", "tube", "well", "circle"}


def test_empty_frame_returns_nothing():
    det = ShapeDetector()
    assert det.detect(np.full((480, 640, 3), 230, np.uint8)) == []
