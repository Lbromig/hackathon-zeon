"""Tests for the top-down world-map scene (core.viz)."""
from __future__ import annotations

from core.viz import scene_svg, world_scene
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy


def _scene_twin():
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")
    wm.set_world_pose("overview_cam", from_xyz_rpy(x=-0.25, y=-0.45, z=0.55))
    wm.set_world_pose("handover_cam", from_xyz_rpy(x=0.25, y=-0.35, z=0.50))
    wm.set_world_pose("ot_base", from_xyz_rpy(x=0.15, y=0.10))
    return wm


def test_world_scene_has_cameras_and_entities():
    scene = world_scene(_scene_twin())
    cam_ids = {c["id"] for c in scene["cameras"]}
    assert {"overview_cam", "handover_cam"} <= cam_ids
    for c in scene["cameras"]:
        assert {"x", "y", "z", "heading"} <= set(c)
    ent_ids = {e["id"] for e in scene["entities"]}
    assert "tube_1" in ent_ids and "ot_base" in ent_ids
    # dense grids are skipped so the map stays readable
    assert not any(e["kind"] in ("well", "tip_site") for e in scene["entities"])


def test_camera_positions_survive_into_scene():
    scene = world_scene(_scene_twin())
    ov = next(c for c in scene["cameras"] if c["id"] == "overview_cam")
    assert round(ov["x"], 3) == -0.25 and round(ov["y"], 3) == -0.45


def test_scene_svg_is_wellformed_and_labels_things():
    svg = scene_svg(world_scene(_scene_twin()))
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "overview_cam" in svg and "handover_cam" in svg and "tube_1" in svg
    assert "world 0,0" in svg          # origin / board marker drawn


def test_scene_svg_handles_empty_scene():
    svg = scene_svg({"cameras": [], "entities": []})
    assert svg.startswith("<svg") and "</svg>" in svg
