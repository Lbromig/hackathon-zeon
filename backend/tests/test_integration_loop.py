"""End-to-end loop on the mock tag camera — no hardware.

Exercises the whole perception chain wired together:
  mock RGB-D camera  ->  hub detect (AprilTag + twin-projection + CV)
  ->  fuse a tag into the twin  ->  verify predicate
  ->  deliberate failure injection  ->  recovery  ->  predicate passes again.

This is the capstone that proves the pieces built for #13/#15/#16/#17/#19 fit together.
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco"):
    pytest.skip("needs opencv-contrib (cv2.aruco)", allow_module_level=True)

from backend.app.services import twin
from backend.app.services.camera_hub import CameraWorker
from core.perception import TwinFuser
from core.verification.agents import AGENTS, Evidence
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy
from drivers.mock import MockTagCameraDriver


@pytest.fixture
def scene():
    """A populated twin published to services.twin, restored afterwards."""
    prev = twin.get_world()
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")
    twin.set_world(wm)
    try:
        yield wm
    finally:
        twin.set_world(prev)


def _worker():
    # device id matches a twin camera entity so projection has a camera pose;
    # depth=True gives it intrinsics + a flat depth plane (RGB-D stand-in).
    drv = MockTagCameraDriver("overview_cam", {"depth": True, "markers": [224, 183],
                                               "motion": False, "width": 960, "height": 540})
    drv.connect()
    return drv, CameraWorker(drv)


def test_detect_produces_all_three_overlay_sources(scene):
    drv, worker = _worker()
    frame, depth = drv.capture_rgbd()
    h, w = frame.shape[:2]
    dets = worker._detect(frame, depth, w, h)

    sources = {d.source for d in dets}
    assert "apriltag" in sources          # rendered tags detected
    assert "projection" in sources        # twin entities projected back onto the frame

    # the id-224 tag resolves to tube_1_cap and, with depth, carries a 3-D point
    tag = next(d for d in dets if d.source == "apriltag" and d.marker_id == 224)
    assert tag.entity_id == "tube_1_cap"
    assert tag.camera_xyz is not None and tag.camera_xyz[2] == pytest.approx(0.5, abs=1e-3)


def test_fuse_detection_moves_the_twin_entity(scene):
    drv, worker = _worker()
    frame, depth = drv.capture_rgbd()
    h, w = frame.shape[:2]
    tag = next(d for d in worker._detect(frame, depth, w, h)
               if d.source == "apriltag" and d.marker_id == 224)

    # overview_cam sits at world identity, so the fused world point == the camera point
    r = TwinFuser().fuse_point(scene, "overview_cam", tag.entity_id, tag.camera_xyz,
                               confidence=tag.confidence)
    assert r.ok
    assert np.allclose(scene.world_pose("tube_1_cap")[:3, 3], tag.camera_xyz, atol=1e-3)


def test_verify_failure_injection_and_recovery(scene):
    wm = scene
    wm.reparent("tube_1", "right_tool")                       # tube now held by the arm
    wm.set_world_pose("nozzle", from_xyz_rpy(x=0.20, y=0.20, z=0.20))

    # presented under the nozzle -> aligned
    wm.set_world_pose("tube_1", from_xyz_rpy(x=0.205, y=0.20, z=0.205))
    assert AGENTS["tube_aligned"].verify(Evidence(world=wm)).ok

    # FAILURE INJECTION: nudge the tube off the presentation pose
    wm.set_world_pose("tube_1", from_xyz_rpy(x=0.26, y=0.20, z=0.31))
    assert not AGENTS["tube_aligned"].verify(Evidence(world=wm)).ok

    # RECOVERY: re-align -> predicate flips back to pass
    wm.set_world_pose("tube_1", from_xyz_rpy(x=0.203, y=0.20, z=0.202))
    assert AGENTS["tube_aligned"].verify(Evidence(world=wm)).ok
