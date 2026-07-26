"""W4: grip/release reparent the twin so verification can pass on a live run."""
from __future__ import annotations

import pytest

from backend.app.services import twin
from backend.app.workflows import uncap_aspirate as ua
from core.verification.agents import AGENTS, Evidence
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy


@pytest.fixture
def scene():
    prev = twin.get_world()
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")
    twin.set_world(wm)
    try:
        yield wm
    finally:
        twin.set_world(prev)


def _apply(step: str) -> None:
    for act in ua.CHOREOGRAPHY[step]:
        ua._apply_twin_effect(act)


def test_uncap_reparents_tube_onto_tool_and_cap_to_dropzone(scene):
    wm = scene
    assert wm.get("tube_1").parent.endswith("_A1")          # starts in its well
    _apply("uncap")
    assert wm.get("tube_1").parent == "left_tool"           # left clamps the tube
    assert wm.get("tube_1_cap").parent == "dropzone"        # cap parked


def test_grasp_secure_passes_after_transport(scene):
    wm = scene
    _apply("uncap")
    _apply("transport")
    assert wm.get("tube_1").parent == "right_tool"          # handed to the right arm
    assert AGENTS["grasp_secure"].verify(Evidence(world=wm)).ok


def test_cap_removed_passes_once_cap_is_carried_clear(scene):
    wm = scene
    _apply("uncap")
    # in a live run arm FK carries the cap to the dropoff before release; emulate that
    wm.set_world_pose("tube_1_cap", from_xyz_rpy(x=0.5, y=0.4, z=0.2))
    r = AGENTS["cap_removed"].verify(Evidence(world=wm))
    assert r.ok and r.data["separation_m"] > 0.02


def test_effect_is_safe_without_twin_or_missing_entity():
    twin.set_world(None)
    ua._apply_twin_effect(ua.Act("left", "grip", attach="tube_1", to="left_tool"))  # no twin
    wm = build_skeleton()
    twin.set_world(wm)
    try:
        ua._apply_twin_effect(ua.Act("left", "grip", attach="ghost", to="left_tool"))  # missing
        assert "ghost" not in wm.entities
    finally:
        twin.set_world(None)
