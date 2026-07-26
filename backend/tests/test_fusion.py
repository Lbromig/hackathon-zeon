"""Tests for core.perception.fusion.TwinFuser (pure, no cameras)."""
from __future__ import annotations

import numpy as np

from core.perception import TwinFuser
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy


def _twin():
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")
    return wm


def test_fuses_point_through_identity_camera():
    wm = _twin()  # overview_cam is world-fixed at identity
    r = TwinFuser().fuse_point(wm, "overview_cam", "tube_1", (0.10, 0.0, 0.50))
    assert r.ok
    assert np.allclose(wm.world_pose("tube_1")[:3, 3], [0.10, 0.0, 0.50], atol=1e-6)


def test_camera_extrinsics_are_applied():
    wm = _twin()
    wm.set_world_pose("overview_cam", from_xyz_rpy(x=1.0))       # camera shifted +1 m in x
    r = TwinFuser().fuse_point(wm, "overview_cam", "tube_1", (0.10, 0.0, 0.50))
    assert r.ok
    assert np.allclose(wm.world_pose("tube_1")[:3, 3], [1.10, 0.0, 0.50], atol=1e-6)


def test_low_confidence_is_dropped():
    wm = _twin()
    before = wm.world_pose("tube_1")[:3, 3].copy()
    r = TwinFuser(min_confidence=0.3).fuse_point(
        wm, "overview_cam", "tube_1", (0.1, 0, 0.5), confidence=0.1)
    assert not r.ok and "confidence" in r.reason
    assert np.allclose(wm.world_pose("tube_1")[:3, 3], before)   # twin untouched


def test_jump_gate_rejects_after_bootstrap():
    wm = _twin()
    f = TwinFuser(max_jump_m=0.1)
    assert f.fuse_point(wm, "overview_cam", "tube_1", (0.0, 0.0, 0.5)).ok      # bootstrap ok
    r = f.fuse_point(wm, "overview_cam", "tube_1", (0.0, 0.0, 1.0))            # +500 mm jump
    assert not r.ok and "jump" in r.reason
    # after reset, the same jump is accepted again
    f.reset("tube_1")
    assert f.fuse_point(wm, "overview_cam", "tube_1", (0.0, 0.0, 1.0)).ok


def test_unknown_entity_or_camera():
    wm = _twin()
    assert not TwinFuser().fuse_point(wm, "overview_cam", "nope", (0, 0, 1)).ok
    assert not TwinFuser().fuse_point(wm, "no_cam", "tube_1", (0, 0, 1)).ok
