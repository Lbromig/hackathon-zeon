"""Verification agents: well-formed results + real twin-query behaviour (no hardware)."""
from core.verification.agents import AGENTS, Evidence, VerificationResult
from core.worldmodel import add_tube_rack, add_tube_with_cap, build_skeleton, from_xyz_rpy


def test_all_agents_registered():
    assert set(AGENTS) == {"cap_removed", "grasp_secure", "tube_aligned", "aspiration_ok"}


def test_agents_return_verification_results():
    ev = Evidence()  # world=None -> fail closed, still well-formed
    for name, agent in AGENTS.items():
        result = agent.verify(ev)
        assert isinstance(result, VerificationResult), name
        assert isinstance(result.ok, bool)
        assert 0.0 <= result.confidence <= 1.0
        assert result.ok is False  # no twin -> cannot confirm anything


# --- behavioural tests against a synthetic twin ------------------------------
def _twin():
    wm = build_skeleton()
    add_tube_rack(wm, "slot_2")
    add_tube_with_cap(wm, "rack_1_A1", "tube_1")   # 50 mL default
    return wm


def _place(wm, eid, x=0.0, y=0.0, z=0.0):
    wm.set_world_pose(eid, from_xyz_rpy(x=x, y=y, z=z))


def test_cap_removed():
    wm = _twin()
    assert AGENTS["cap_removed"].verify(Evidence(world=wm)).ok is False  # still capped

    _place(wm, "tube_1", x=0.30, z=0.10)
    wm.reparent("tube_1_cap", "dropzone", keep_world_pose=False)
    _place(wm, "tube_1_cap", x=0.30, y=0.10, z=0.10)   # 100 mm from the tube
    off = AGENTS["cap_removed"].verify(Evidence(world=wm))
    assert off.ok is True and off.data["separation_m"] > 0.02


def test_grasp_secure_with_telemetry():
    wm = _twin()
    assert AGENTS["grasp_secure"].verify(Evidence(world=wm)).ok is False  # in the well

    wm.reparent("tube_1", "right_tool")
    held = AGENTS["grasp_secure"].verify(Evidence(world=wm))
    assert held.ok is True

    ev = Evidence(world=wm, telemetry={"right": {"gripper_width": 0.026}})  # Ø28 mm tube
    strong = AGENTS["grasp_secure"].verify(ev)
    assert strong.ok is True and strong.confidence > held.confidence


def test_tube_aligned():
    wm = _twin()
    wm.reparent("tube_1", "right_tool")
    _place(wm, "nozzle", x=0.20, y=0.20, z=0.20)

    _place(wm, "tube_1", x=0.20, y=0.20, z=0.30)        # 100 mm off
    assert AGENTS["tube_aligned"].verify(Evidence(world=wm)).ok is False

    _place(wm, "tube_1", x=0.205, y=0.20, z=0.205)      # ~7 mm off
    near = AGENTS["tube_aligned"].verify(Evidence(world=wm))
    assert near.ok is True and near.confidence > 0.0


def test_aspiration_ok():
    wm = _twin()
    assert AGENTS["aspiration_ok"].verify(Evidence(world=wm)).ok is False

    ev = Evidence(world=wm, telemetry={"ot": {"aspirated_volume_ul": 100.0}})
    done = AGENTS["aspiration_ok"].verify(ev)
    assert done.ok is True and done.data["aspirated_volume_ul"] == 100.0
