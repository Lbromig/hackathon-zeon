"""Tests for core.perception.projection (pure numpy, no cameras)."""
from __future__ import annotations

import numpy as np

from core.perception import project_entity, project_twin
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy


def _twin():
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")
    return wm


def _K(w=640, h=480, f=600.0):
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float), w, h


def test_projects_entity_on_optical_axis_to_center():
    wm = _twin()                                   # overview_cam at world identity (+Z fwd)
    K, w, h = _K()
    wm.set_world_pose("tube_1", from_xyz_rpy(z=0.5))   # 0.5 m straight ahead of the cam
    got = project_entity(wm, "overview_cam", "tube_1", K, w, h)
    assert got is not None
    assert got["source"] == "projection" and got["kind"] == "tube"
    assert len(got["polygon"]) >= 3
    # centred on the optical axis -> near image centre, and normalized to [0,1]
    assert abs(got["center"][0] - 0.5) < 0.1 and abs(got["center"][1] - 0.5) < 0.1
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in got["polygon"])


def test_entity_behind_camera_is_culled():
    wm = _twin()
    K, w, h = _K()
    wm.set_world_pose("tube_1", from_xyz_rpy(z=-0.5))   # behind the camera
    assert project_entity(wm, "overview_cam", "tube_1", K, w, h) is None


def test_project_twin_returns_known_kinds():
    wm = _twin()
    K, w, h = _K()
    # put the whole scene in front of the camera
    for eid in ("tube_1", "tube_1_cap", "nozzle"):
        wm.set_world_pose(eid, from_xyz_rpy(x=0.02, z=0.5))
    got = project_twin(wm, "overview_cam", K, w, h)
    kinds = {g["kind"] for g in got}
    assert {"tube", "cap"} <= kinds          # nozzle may sit outside the frame; tube+cap do
    assert all(g["source"] == "projection" for g in got)
