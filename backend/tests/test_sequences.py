"""User-built action sequences.

Two properties carry the safety of this feature: the whole sequence is checked before
the first move, and a step that cannot run raises instead of being skipped. A skipped
step reads downstream as "the sequence ran", which is how a robot reports success having
done nothing.
"""
from __future__ import annotations

import json

import pytest

import drivers.mock  # noqa: F401
from core import sequences
from core.config import settings
from drivers import build_driver


def _arms():
    out = {}
    for i in ("left", "right"):
        a = build_driver({"type": "mock_arm", "id": i})
        a.connect()
        out[i] = a
    return out


def _teach(names, device="right"):
    """Write a pose library containing `names`, as the teach panel would."""
    lib = {device: {n: {"name": n, "joints": [0.0] * 6,
                        "pose": {"x": 200.0, "y": 0.0, "z": 300.0,
                                 "roll": 180.0, "pitch": 0.0, "yaw": 0.0}}
                    for n in names}}
    with open(settings.teach_poses_file, "w") as f:
        json.dump(lib, f)


def _rack_to_ot() -> sequences.Sequence:
    """The route the operator described: tube rack -> Opentrons deck."""
    s = sequences.Step
    return sequences.Sequence(name="rack_to_ot", steps=[
        s("move", "right", pose="rack_approach"),
        s("ungrip", "right"),
        s("move", "right", pose="rack_grasp"),
        s("grip", "right", width=120.0),
        s("move", "right", pose="rack_lift"),
        s("move", "right", pose="transit_safe"),
        s("move", "right", pose="ot_approach"),
        s("move", "right", pose="ot_nest"),
        s("ungrip", "right"),
        s("move", "right", pose="ot_retreat"),
    ])


_ROUTE_POSES = ["rack_approach", "rack_grasp", "rack_lift", "transit_safe",
                "ot_approach", "ot_nest", "ot_retreat"]


# --- validation ---------------------------------------------------------------

def test_move_without_a_pose_is_rejected():
    with pytest.raises(sequences.SequenceError, match="taught pose name"):
        sequences.validate_step(sequences.Step("move", "right"))


def test_unknown_action_is_rejected():
    with pytest.raises(sequences.SequenceError, match="unknown action"):
        sequences.validate_step(sequences.Step("teleport", "right", pose="x"))


def test_unscrew_needs_at_least_one_bite():
    with pytest.raises(sequences.SequenceError, match="180"):
        sequences.validate_step(sequences.Step("unscrew", "right", half_turns=0))


# --- pre-flight ----------------------------------------------------------------

def test_preflight_names_every_untaught_pose():
    problems = sequences.preflight(_rack_to_ot(), _arms())
    assert len(problems) == len(_ROUTE_POSES)
    assert any("rack_approach" in p for p in problems)


def test_preflight_passes_once_the_route_is_taught():
    _teach(_ROUTE_POSES)
    assert sequences.preflight(_rack_to_ot(), _arms()) == []


def test_preflight_reports_a_missing_device():
    seq = sequences.Sequence(name="s", steps=[sequences.Step("ungrip", "nope")])
    problems = sequences.preflight(seq, _arms())
    assert problems and "nope" in problems[0]


def test_preflight_rejects_a_pose_outside_the_joint_limits():
    _teach(["far"])
    with open(settings.teach_poses_file) as f:
        lib = json.load(f)
    lib["right"]["far"]["joints"] = [0.0, 0.0, 0.0, 0.0, 200.0, 0.0]   # J5 way out
    with open(settings.teach_poses_file, "w") as f:
        json.dump(lib, f)

    arms = _arms()
    arms["right"] = build_driver({"type": "mock_arm", "id": "right",
                                  "limits": {"joints": [[-360, 360]] * 4
                                             + [[-90, 90]] + [[-360, 360]]}})
    arms["right"].connect()
    seq = sequences.Sequence(name="s", steps=[sequences.Step("move", "right", pose="far")])
    problems = sequences.preflight(seq, arms)
    assert problems and "J5" in problems[0]


# --- execution -----------------------------------------------------------------

def test_running_the_route_executes_every_step():
    _teach(_ROUTE_POSES)
    arms = _arms()
    events = list(sequences.run(_rack_to_ot(), arms))
    assert events[-1]["phase"] == "complete"
    done = [e for e in events if e["phase"] == "done"]
    assert len(done) == 10


def test_run_stops_at_the_first_failure():
    _teach(_ROUTE_POSES[:2])          # only the first two waypoints exist
    events = list(sequences.run(_rack_to_ot(), _arms(), skip_preflight=True))
    assert events[-1]["phase"] == "failed"
    assert "rack_lift" in events[-1]["detail"]
    assert not any(e["phase"] == "done" and e["index"] > 5 for e in events)


def test_run_refuses_up_front_when_preflight_fails():
    """Nothing should move if the route is incomplete."""
    events = list(sequences.run(_rack_to_ot(), _arms()))
    assert len(events) == 1
    assert events[0]["phase"] == "preflight" and events[0]["ok"] is False


def test_a_missing_pose_raises_rather_than_being_skipped():
    with pytest.raises(Exception) as e:
        sequences.run_step(sequences.Step("move", "right", pose="nope"), _arms()["right"])
    assert "nope" in str(e.value)


def test_grip_and_ungrip_drive_the_gripper():
    arms = _arms()
    sequences.run_step(sequences.Step("ungrip", "right"), arms["right"])
    assert arms["right"].gripper_width() == pytest.approx(850.0)
    sequences.run_step(sequences.Step("grip", "right", width=120.0), arms["right"])
    assert arms["right"].gripper_width() == pytest.approx(120.0)


def test_unscrew_step_runs_the_ratchet_and_returns_the_wrist():
    arms = _arms()
    before = arms["right"].get_joints()
    sequences.run_step(sequences.Step("unscrew", "right", half_turns=2), arms["right"])
    assert arms["right"].get_joints() == pytest.approx(before)


def test_move_prefers_the_recorded_joints():
    _teach(["p"])
    arms = _arms()
    detail = sequences.run_step(sequences.Step("move", "right", pose="p"), arms["right"])
    assert "joint replay" in detail


# --- persistence ---------------------------------------------------------------

def test_round_trip_through_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HZ_SEQUENCES_FILE", str(tmp_path / "seq.json"))
    sequences.save(_rack_to_ot())
    back = sequences.get("rack_to_ot")
    assert [s.describe() for s in back.steps] == [s.describe() for s in _rack_to_ot().steps]


def test_saving_an_invalid_step_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HZ_SEQUENCES_FILE", str(tmp_path / "seq.json"))
    bad = sequences.Sequence(name="bad", steps=[sequences.Step("move", "right")])
    with pytest.raises(sequences.SequenceError):
        sequences.save(bad)
    assert sequences.load() == {}, "a rejected sequence must not be written"


def test_delete_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HZ_SEQUENCES_FILE", str(tmp_path / "seq.json"))
    sequences.save(_rack_to_ot())
    assert sequences.delete("rack_to_ot") is True
    assert sequences.delete("rack_to_ot") is False
