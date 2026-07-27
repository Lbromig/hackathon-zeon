"""Teach/jog API — the safety rules, exercised against the mock fleet.

These are the guarantees the UI relies on but must never be trusted to enforce:
bounded increments, no motion on a faulted arm, one command in flight per arm,
and an e-stop that is never queued behind the move it is interrupting.
"""
import threading
import time

import drivers.mock  # noqa: F401  -- registers the mock driver types
import pytest
from fastapi.testclient import TestClient

from backend.app.api import teach

FLEET = [
    {"type": "mock_arm", "id": "left", "name": "Left arm",
     "limits": {"joints": [[-360, 360]] * 6, "max_jog_linear": 50, "max_jog_angular": 15,
                "max_speed_linear": 200, "max_speed_angular": 60, "max_move_to_jump": 250}},
    {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    from core.config import settings

    monkeypatch.setattr(settings, "fleet", FLEET)
    monkeypatch.setattr(settings, "teach_poses_file", str(tmp_path / "teach_poses.json"))

    from backend.app.main import app

    with TestClient(app) as c:  # context triggers the lifespan (load_fleet)
        c.post("/api/instruments/left/connect")
        yield c


def arm(client):
    """The live driver instance, for asserting on what actually moved."""
    from backend.app.services.device_manager import device_manager

    return device_manager.get("left")


# --- discovery ---------------------------------------------------------------

def test_lists_only_arms(client):
    arms = client.get("/api/arms").json()
    assert [a["id"] for a in arms] == ["left"]      # the liquid handler is not an arm
    assert arms[0]["gripper"]["kind"] == "parallel"
    assert arms[0]["limits"]["joints"]
    assert client.get("/api/arms/ot/state").status_code == 400
    assert client.get("/api/arms/nope/state").status_code == 404


def test_state_reports_pose_and_joints(client):
    state = client.get("/api/arms/left/state").json()
    assert state["connected"] and not state["busy"]
    assert state["pose"]["x"] == pytest.approx(200.0)
    assert len(state["joints"]) == 6


# --- jogging -----------------------------------------------------------------

def test_cartesian_jog_moves_one_increment(client):
    before = arm(client).get_pose().x
    body = client.post("/api/arms/left/jog",
                       json={"space": "cartesian", "axis": "x", "delta": 1.5}).json()
    assert body["ok"], body["detail"]
    assert arm(client).get_pose().x == pytest.approx(before + 1.5)


def test_joint_jog_moves_only_that_joint(client):
    body = client.post("/api/arms/left/jog",
                       json={"space": "joint", "axis": "j3", "delta": 5}).json()
    assert body["ok"], body["detail"]
    assert arm(client).get_joints() == [0, 0, 5, 0, 0, 0]


@pytest.mark.parametrize("payload", [
    {"space": "cartesian", "axis": "x", "delta": 999},      # over max_jog_linear
    {"space": "cartesian", "axis": "roll", "delta": 90},    # over max_jog_angular
    {"space": "cartesian", "axis": "w", "delta": 1},        # not an axis
    {"space": "joint", "axis": "j9", "delta": 1},           # arm has 6
    {"space": "joint", "axis": "elbow", "delta": 1},        # not a joint name
])
def test_bad_jogs_are_refused_without_moving(client, payload):
    before = (arm(client).get_pose().__dict__, arm(client).get_joints())
    body = client.post("/api/arms/left/jog", json=payload).json()
    assert not body["ok"]
    assert (arm(client).get_pose().__dict__, arm(client).get_joints()) == before


def test_speed_is_capped_not_rejected(client):
    body = client.post("/api/arms/left/jog",
                       json={"space": "cartesian", "axis": "x", "delta": 1, "speed": 99999}).json()
    assert body["ok"], body["detail"]


@pytest.mark.parametrize("raw", [
    '{"space":"cartesian","axis":"x","delta":NaN}',
    '{"space":"cartesian","axis":"x","delta":Infinity}',
    '{"space":"cartesian","axis":"x","delta":1,"speed":NaN}',
])
def test_non_finite_input_is_rejected_cleanly(client, raw):
    """NaN defeats every `value > limit` guard, so it must die at the boundary.

    The response must still be a readable 422 — the stock handler echoes the
    rejected value back and cannot serialize a NaN, turning this into a 500.
    """
    before = arm(client).get_pose().__dict__
    r = client.post("/api/arms/left/jog", data=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "finite" in r.text
    assert arm(client).get_pose().__dict__ == before


# --- absolute moves ----------------------------------------------------------

def test_move_to_requires_exactly_one_target(client):
    assert client.post("/api/arms/left/move_to", json={}).status_code == 400
    assert client.post("/api/arms/left/move_to",
                       json={"pose": {"x": 1, "y": 2, "z": 3}, "joints": [0] * 6}).status_code == 400


def test_far_cartesian_move_is_refused(client):
    """A typo'd coordinate must not become a 900 mm swing through the deck."""
    before = arm(client).get_pose().x
    body = client.post("/api/arms/left/move_to", json={"pose": {"x": 900, "y": 0, "z": 200}}).json()
    assert not body["ok"] and "limit" in body["detail"]
    assert arm(client).get_pose().x == pytest.approx(before)


def test_nearby_cartesian_move_is_allowed(client):
    here = arm(client).get_pose().__dict__
    body = client.post("/api/arms/left/move_to", json={"pose": {**here, "x": 220}}).json()
    assert body["ok"], body["detail"]
    assert arm(client).get_pose().x == pytest.approx(220)


def test_large_rotation_is_refused_when_a_cap_is_configured(client, monkeypatch):
    """Same xyz, flipped wrist, is still a big unplanned swing — IF a cap is set.

    The cap is lifted by default (2026-07-26 operator decision: it refused real
    waypoint-to-waypoint moves), so this pins it explicitly rather than relying on the
    default. The guard itself still has to work for anyone who restores it.
    """
    monkeypatch.setattr(teach, "MAX_MOVE_TO_ROTATION_DEG", 90.0)
    here = arm(client).get_pose().__dict__
    body = client.post("/api/arms/left/move_to",
                       json={"pose": {**here, "yaw": here["yaw"] + 170}}).json()
    assert not body["ok"] and "rotates" in body["detail"]
    assert arm(client).get_pose().yaw == pytest.approx(here["yaw"])


def test_a_large_rotation_is_allowed_by_default(client):
    """The default is no cap: a waypoint-to-waypoint move may rotate freely."""
    here = arm(client).get_pose().__dict__
    body = client.post("/api/arms/left/move_to",
                       json={"pose": {**here, "yaw": here["yaw"] + 84}}).json()
    assert body["ok"], body
    assert arm(client).get_pose().yaw == pytest.approx(here["yaw"] + 84)


def test_a_far_cartesian_target_is_allowed_when_the_cap_is_lifted(client):
    """The bench case: 563 mm between two taught waypoints, previously refused at 250 mm.

    The FLEET fixture still pins 250 so `test_far_cartesian_move_is_refused` keeps testing
    the guard; this states the shipped default (100 m, i.e. no cap) on the arm under test.
    """
    arm(client).limits.max_move_to_jump = 100_000.0
    here = arm(client).get_pose().__dict__
    body = client.post("/api/arms/left/move_to",
                       json={"pose": {**here, "x": here["x"] + 563.5}}).json()
    assert body["ok"], body
    assert arm(client).get_pose().x == pytest.approx(here["x"] + 563.5)


def test_joint_target_outside_limits_is_refused(client):
    body = client.post("/api/arms/left/move_to", json={"joints": [999, 0, 0, 0, 0, 0]}).json()
    assert not body["ok"] and "soft limit" in body["detail"]
    assert arm(client).get_joints() == [0] * 6


def test_joint_target_wrong_length_is_refused(client):
    assert not client.post("/api/arms/left/move_to", json={"joints": [0, 0, 0]}).json()["ok"]


# --- gripper -----------------------------------------------------------------

def test_gripper_open_close_and_width(client):
    assert client.post("/api/arms/left/gripper", json={"action": "close"}).json()["ok"]
    assert arm(client).gripper_width() == 0
    assert client.post("/api/arms/left/gripper", json={"action": "open"}).json()["ok"]
    assert arm(client).gripper_width() == 850
    assert client.post("/api/arms/left/gripper", json={"action": "set", "width": 400}).json()["ok"]
    assert arm(client).gripper_width() == 400


def test_out_of_range_width_is_rejected_not_clamped(client):
    """Clamping is how a metres/counts mix-up silently becomes a crushed tube."""
    client.post("/api/arms/left/gripper", json={"action": "set", "width": 400})
    body = client.post("/api/arms/left/gripper", json={"action": "set", "width": 99999}).json()
    assert not body["ok"] and "outside" in body["detail"]
    assert arm(client).gripper_width() == 400


def test_set_requires_a_width(client):
    assert not client.post("/api/arms/left/gripper", json={"action": "set"}).json()["ok"]


# --- faults ------------------------------------------------------------------

def test_motion_is_refused_while_a_fault_is_latched(client, monkeypatch):
    monkeypatch.setattr(arm(client), "status", lambda: {"error_code": 31, "warn_code": 0})
    before = arm(client).get_pose().x

    body = client.post("/api/arms/left/jog",
                       json={"space": "cartesian", "axis": "x", "delta": 1}).json()
    assert not body["ok"] and "error 31" in body["detail"]
    assert arm(client).get_pose().x == pytest.approx(before)
    assert client.get("/api/arms/left/state").json()["error_code"] == 31


def test_motion_is_refused_when_fault_state_cannot_be_read(client, monkeypatch):
    """Fails closed: an unreadable arm is not a healthy arm."""
    def boom():
        raise RuntimeError("socket gone")

    monkeypatch.setattr(arm(client), "status", boom)
    body = client.post("/api/arms/left/jog",
                       json={"space": "cartesian", "axis": "x", "delta": 1}).json()
    assert not body["ok"] and "refusing to move" in body["detail"]


def test_disconnected_arm_refuses_motion_without_erroring(client):
    client.post("/api/arms/left/enable", json={"on": False})   # mock: disable == disconnect
    body = client.post("/api/arms/left/jog",
                       json={"space": "cartesian", "axis": "x", "delta": 1}).json()
    assert not body["ok"] and "not connected" in body["detail"]


# --- taught poses ------------------------------------------------------------

def test_pose_is_saved_and_replayed_in_joint_space(client):
    client.post("/api/arms/left/jog", json={"space": "joint", "axis": "j2", "delta": 10})
    taught = arm(client).get_joints()

    saved = client.post("/api/arms/left/poses", json={"name": "rack_a1", "note": "above A1"}).json()
    assert [p["name"] for p in saved] == ["rack_a1"]
    assert saved[0]["joints"] == taught and saved[0]["pose"] is not None

    client.post("/api/arms/left/jog", json={"space": "joint", "axis": "j2", "delta": -10})
    body = client.post("/api/arms/left/poses/rack_a1/goto").json()
    assert body["ok"], body["detail"]
    assert arm(client).get_joints() == taught      # exact replay, no IK round-trip


def test_poses_are_namespaced_per_device(client):
    client.post("/api/arms/left/poses", json={"name": "p1"})
    assert client.get("/api/arms/left/poses").json()
    # a pose taught on one arm must not be reachable on another device id
    assert client.post("/api/arms/ot/poses/p1/goto").status_code in (400, 404)


def test_missing_pose_is_a_404(client):
    assert client.post("/api/arms/left/poses/nope/goto").status_code == 404
    assert client.delete("/api/arms/left/poses/nope").status_code == 404


def test_pose_survives_a_restart(client):
    client.post("/api/arms/left/poses", json={"name": "keepme"})

    from backend.app.main import app

    with TestClient(app) as second:      # fresh lifespan, same poses file
        assert [p["name"] for p in second.get("/api/arms/left/poses").json()] == ["keepme"]


def test_delete_removes_the_pose(client):
    client.post("/api/arms/left/poses", json={"name": "p1"})
    assert client.delete("/api/arms/left/poses/p1").json() == []


# --- concurrency -------------------------------------------------------------

def test_one_command_in_flight_per_arm(client, monkeypatch):
    """Two concurrent SDK motion calls on one arm is a hazard; click-spam causes it."""
    real = arm(client).move_relative

    def slow(*a, **kw):
        time.sleep(0.4)
        return real(*a, **kw)

    monkeypatch.setattr(arm(client), "move_relative", slow)
    t = threading.Thread(target=lambda: client.post(
        "/api/arms/left/jog", json={"space": "cartesian", "axis": "x", "delta": 1}))
    t.start()
    try:
        time.sleep(0.15)
        busy = client.post("/api/arms/left/jog", json={"space": "cartesian", "axis": "y", "delta": 1})
        assert busy.status_code == 409

        # ...but the e-stop must never queue behind the move it is interrupting
        assert client.post("/api/arms/left/stop", json={"emergency": True}).status_code == 200
    finally:
        t.join()

    assert client.get("/api/arms/left/state").json()["busy"] is False
