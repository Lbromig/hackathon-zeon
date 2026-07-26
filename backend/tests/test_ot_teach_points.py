"""Teach points on the liquid handler, and the datum rule that guards replay.

The rule under test is the one that matters on this machine: a taught coordinate
means nothing unless the datum it was measured against still exists. The OT-One's
counters are zeroed by a power cycle (observed: X read 300 before a replug and 0
after, carriage unmoved), so replaying a point across that boundary would drive
at full speed to a place that is no longer where it was, with no endstop to stop
it. These tests pin the refusals, not just the happy path.
"""
import drivers.mock  # noqa: F401  -- registers the mock driver types
import pytest
from fastapi.testclient import TestClient

FLEET = [
    {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
    {"type": "mock_arm", "id": "left", "name": "Left arm"},
]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from core.config import settings

    monkeypatch.setattr(settings, "fleet", FLEET)
    monkeypatch.setattr(settings, "teach_poses_file", str(tmp_path / "teach_poses.json"))

    from backend.app.main import app

    with TestClient(app) as c:
        yield c


def test_point_saves_and_lists_back(client):
    r = client.post("/api/liquid-handlers/ot/points", json={"name": "tiprack_a1"})
    assert r.status_code == 200, r.text
    names = [p["name"] for p in r.json()]
    assert names == ["tiprack_a1"]

    listed = client.get("/api/liquid-handlers/ot/points").json()
    assert listed[0]["pose"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert listed[0]["saved_at"]


def test_unhomed_point_records_its_datum_as_unhomed(client):
    saved = client.post("/api/liquid-handlers/ot/points",
                        json={"name": "p", "note": "before homing"}).json()[0]
    assert saved["datum"]["homed"] is False
    # Y can never be referenced on this unit, homed or not.
    assert saved["datum"]["y_referenced"] is False


def test_goto_refuses_a_point_taught_without_a_datum(client):
    client.post("/api/liquid-handlers/ot/points", json={"name": "p"})
    client.post("/api/liquid-handlers/ot/home")          # datum now exists...
    r = client.post("/api/liquid-handlers/ot/points/p/goto")
    # ...but the point predates it, so its coordinates are still meaningless.
    assert r.status_code == 409
    assert "without a homed datum" in r.json()["detail"]


def test_goto_refuses_when_this_session_was_never_homed(client):
    """The mirror case: the point is good, the machine's current zero is not."""
    client.post("/api/liquid-handlers/ot/home")
    client.post("/api/liquid-handlers/ot/jog", json={"axis": "X", "delta": 5})
    client.post("/api/liquid-handlers/ot/points", json={"name": "p"})

    # Simulate the power cycle: counters zeroed, homed flag cleared.
    from backend.app.services.device_manager import device_manager
    dev = device_manager.get("ot")
    dev._homed = False
    dev._pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}

    r = client.post("/api/liquid-handlers/ot/points/p/goto")
    assert r.status_code == 409
    assert "not been homed in this session" in r.json()["detail"]


def test_goto_replays_x_and_z_but_never_y(client):
    from backend.app.services.device_manager import device_manager

    client.post("/api/liquid-handlers/ot/home")
    dev = device_manager.get("ot")
    dev._pos = {"X": 120.0, "Y": 40.0, "Z": 15.0}
    client.post("/api/liquid-handlers/ot/points", json={"name": "well"})

    dev._pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    r = client.post("/api/liquid-handlers/ot/points/well/goto")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # X and Z are restored; Y is deliberately left where it was, because a Y
    # coordinate cannot be returned to on a machine whose Y never homes.
    assert dev._pos["X"] == 120.0
    assert dev._pos["Z"] == 15.0
    assert dev._pos["Y"] == 0.0
    assert "Y not replayed" in r.json()["detail"]


def test_delete_and_missing_point(client):
    client.post("/api/liquid-handlers/ot/points", json={"name": "p"})
    assert client.delete("/api/liquid-handlers/ot/points/p").json() == []
    assert client.delete("/api/liquid-handlers/ot/points/p").status_code == 404
    assert client.post("/api/liquid-handlers/ot/points/nope/goto").status_code == 404


def test_points_are_refused_on_a_non_liquid_handler(client):
    assert client.get("/api/liquid-handlers/left/points").status_code == 400


def test_an_empty_name_is_refused(client):
    r = client.post("/api/liquid-handlers/ot/points", json={"name": "   "})
    assert r.status_code == 400
