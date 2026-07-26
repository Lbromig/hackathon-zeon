"""The waypoint half of the teach API: the checklist, and ownership at save/replay time.

Two arms in the fleet here, unlike `test_teach_api.py`'s one — every interesting case in
this file is about the *pair* of them, and a single-arm fleet cannot express the mistake
these endpoints exist to catch.
"""
from __future__ import annotations

import json

import drivers.mock  # noqa: F401  -- registers the mock driver types
import pytest
from fastapi.testclient import TestClient

from core import waypoints

LIMITS = {"joints": [[-360, 360]] * 6, "max_jog_linear": 50, "max_jog_angular": 15,
          "max_speed_linear": 200, "max_speed_angular": 60, "max_move_to_jump": 250}
FLEET = [
    {"type": "mock_arm", "id": "left", "name": "Left arm", "limits": LIMITS},
    {"type": "mock_arm", "id": "right", "name": "Right arm", "limits": LIMITS},
]


@pytest.fixture
def poses_file(isolated_teach_poses):
    """The isolated library, plus a hard guarantee that it *is* isolated.

    Two tests here call `teach._write_poses` directly to exercise the atomic-write path,
    and that function writes wherever `settings.teach_poses_file` points. This fixture
    asserts, before and after, that it points inside pytest's tmp_path — anything that
    quietly undoes the isolation (a `monkeypatch.undo()`, a fixture ordering change) would
    otherwise overwrite the bench's real taught poses, which are gitignored and
    unrecoverable. Learned the hard way.
    """
    from core.config import settings

    def isolated() -> bool:
        return str(isolated_teach_poses) == settings.teach_poses_file

    assert isolated(), "the teach-pose library is not isolated; refusing to run"
    yield isolated_teach_poses
    assert isolated(), "a test un-isolated settings.teach_poses_file — see the docstring"


@pytest.fixture
def client(monkeypatch, poses_file):
    from core.config import settings

    monkeypatch.setattr(settings, "fleet", FLEET)

    from backend.app.main import app

    with TestClient(app) as c:
        c.post("/api/instruments/left/connect")
        c.post("/api/instruments/right/connect")
        yield c


def library(poses_file) -> dict:
    return json.loads(poses_file.read_text())


# --- the checklist -----------------------------------------------------------------

def test_report_lists_every_workflow_waypoint_per_arm(client):
    body = client.get("/api/arms/waypoints").json()
    assert body["total"] == 15 and body["taught"] == 0
    by_device = {d["device"]: d for d in body["devices"]}
    assert set(by_device) == {"left", "right"}
    assert by_device["right"]["total"] == 10 and by_device["left"]["total"] == 5


def test_report_carries_step_tier_and_note_for_each_row(client):
    body = client.get("/api/arms/right/waypoints").json()
    rows = {r["name"]: r for r in body["waypoints"]}
    assert rows["TUBE"]["speed"] == "slow"
    assert rows["TUBE"]["step"] == 3
    assert rows["TUBE"]["note"]
    assert rows["TUBE"]["taught"] is False and rows["TUBE"]["saved_at"] is None
    # In workflow order, so the operator can work down the list.
    assert [r["step"] for r in body["waypoints"]] == sorted(r["step"] for r in body["waypoints"])


def test_a_pickers_list_never_contains_the_other_arms_waypoints(client):
    """R-WP-3 at the source: the endpoint the picker is built from cannot express an
    invalid pairing, because it only ever emits one arm's own names."""
    right = {r["name"] for r in client.get("/api/arms/right/waypoints").json()["waypoints"]}
    left = {r["name"] for r in client.get("/api/arms/left/waypoints").json()["waypoints"]}
    assert right & left == {"HOME"}          # the only shared name (R-WP-4)
    assert "CAP_GRAB" in left and "CAP_GRAB" not in right
    assert "TRANSITION_MID_TABLE" in right and "TRANSITION_MID_TABLE" not in left


def test_checklist_progress_advances_as_waypoints_are_taught(client):
    assert client.get("/api/arms/right/waypoints").json()["taught"] == 0
    for name in ("APPROACH_RACK", "APPROACH_TUBE_GRAB", "TUBE"):
        assert client.post("/api/arms/right/poses", json={"name": name}).status_code == 200
    body = client.get("/api/arms/right/waypoints").json()
    assert (body["taught"], body["total"], body["complete"]) == (3, 10, False)
    rows = {r["name"]: r for r in body["waypoints"]}
    assert rows["TUBE"]["taught"] and rows["TUBE"]["has_joints"]
    assert rows["TUBE"]["saved_at"]


def test_the_checklist_is_available_before_the_arm_is_connected(monkeypatch, poses_file):
    """The operator opens the teach tab, sees what has to be taught, *then* connects."""
    from core.config import settings

    monkeypatch.setattr(settings, "fleet", FLEET)
    from backend.app.main import app

    with TestClient(app) as c:
        assert c.get("/api/arms/right/waypoints").json()["total"] == 10


def test_an_unreadable_library_is_a_stated_reason_not_an_empty_checklist(client, poses_file):
    poses_file.write_text("{ truncated")
    r = client.get("/api/arms/waypoints")
    assert r.status_code == 503
    assert "unreadable" in r.json()["detail"]


# --- ownership at save time --------------------------------------------------------

def test_saving_a_waypoint_the_arm_does_not_own_is_refused(client):
    r = client.post("/api/arms/right/poses", json={"name": "CAP_GRAB"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "'left'" in detail and "'right'" in detail       # owner and actor, both named
    assert "CAP_GRAB" in detail


def test_the_refused_waypoint_is_not_written_to_disk(client, poses_file):
    client.post("/api/arms/right/poses", json={"name": "TRANSITION_MID_TABLE"})  # legal
    client.post("/api/arms/left/poses", json={"name": "TRANSITION_MID_TABLE"})   # refused
    assert list(library(poses_file)["right"]) == ["TRANSITION_MID_TABLE"]
    assert "left" not in library(poses_file)


def test_home_can_be_taught_on_both_arms_independently(client, poses_file):
    for device in ("left", "right"):
        assert client.post(f"/api/arms/{device}/poses", json={"name": "HOME"}).status_code == 200
    stored = library(poses_file)
    assert "HOME" in stored["left"] and "HOME" in stored["right"]
    # Two entries, not one shared one (R-WP-4).
    assert stored["left"]["HOME"] is not stored["right"]["HOME"]


def test_free_form_scratch_points_are_still_allowed(client):
    assert client.post("/api/arms/right/poses",
                       json={"name": "rack_A1_above", "note": "scratch"}).status_code == 200
    body = client.get("/api/arms/right/waypoints").json()
    assert body["taught"] == 0                    # not a spec waypoint
    assert body["extra"] == ["rack_A1_above"]     # but visibly present


def test_a_scratch_point_is_reported_as_a_non_blocking_problem(client):
    client.post("/api/arms/right/poses", json={"name": "rack_A1_above"})
    problems = client.get("/api/arms/waypoints").json()["problems"]
    ad_hoc = [p for p in problems if p["kind"] == "ad_hoc"]
    assert [p["name"] for p in ad_hoc] == ["rack_A1_above"]
    assert not ad_hoc[0]["blocking"]


def test_a_hand_edited_wrong_device_waypoint_is_reported_as_blocking(client, poses_file):
    """The API refuses to create this, but a hand-edited file can still contain it, and it
    is the one that silently reads as taught."""
    poses_file.write_text(json.dumps({"right": {"CAP_GRAB": {
        "name": "CAP_GRAB", "pose": {"x": 1.0, "y": 0.0, "z": 0.0,
                                     "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "joints": [0.0] * 6, "gripper_width": None, "note": "", "saved_at": "x"}}}))
    problems = client.get("/api/arms/waypoints").json()["problems"]
    wrong = [p for p in problems if p["kind"] == "wrong_device"]
    assert [(p["device"], p["name"]) for p in wrong] == [("right", "CAP_GRAB")]
    assert wrong[0]["blocking"]


# --- ownership at replay time ------------------------------------------------------

def test_replaying_a_waypoint_the_arm_does_not_own_is_refused(client, poses_file):
    """A wrong-device pose on disk must not be replayable — this is the move that would
    drive one arm to the other's taught point."""
    poses_file.write_text(json.dumps({"right": {"CAP_GRAB": {
        "name": "CAP_GRAB", "pose": {"x": 200.0, "y": 0.0, "z": 300.0,
                                     "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
        "joints": [0.0] * 6, "gripper_width": None, "note": "", "saved_at": "x"}}}))
    r = client.post("/api/arms/right/poses/CAP_GRAB/goto")
    assert r.status_code == 400
    assert "'left'" in r.json()["detail"] and "'right'" in r.json()["detail"]


def test_a_wrong_device_waypoint_can_still_be_deleted(client, poses_file):
    """Refusing to *move* to it must not also make it impossible to clean up."""
    poses_file.write_text(json.dumps({"right": {"CAP_GRAB": {
        "name": "CAP_GRAB", "pose": None, "joints": [0.0] * 6,
        "gripper_width": None, "note": "", "saved_at": "x"}}}))
    assert client.delete("/api/arms/right/poses/CAP_GRAB").status_code == 200
    assert library(poses_file)["right"] == {}


def test_goto_replays_at_the_named_tier(client):
    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    r = client.post("/api/arms/right/poses/TUBE/goto?tier=slow")
    assert r.json()["ok"], r.json()["detail"]
    assert "joint replay" in r.json()["detail"]


def test_an_unknown_tier_still_moves_rather_than_failing_the_replay(client):
    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    r = client.post("/api/arms/right/poses/TUBE/goto?tier=warp")
    assert r.json()["ok"], r.json()["detail"]


def test_goto_of_an_untaught_waypoint_is_a_404_naming_the_arm(client):
    r = client.post("/api/arms/right/poses/TUBE/goto")
    assert r.status_code == 404
    assert "right" in r.json()["detail"]


# --- persistence (R-WP-6/7/8) ------------------------------------------------------

def test_a_saved_waypoint_is_on_disk_before_the_request_returns(client, poses_file):
    """R-WP-6. Asserted against the file, not against the response: a response built from
    an in-memory copy would pass an end-to-end check and lose the pose on restart."""
    client.post("/api/arms/right/poses", json={"name": "TUBE", "note": "on the tube"})
    stored = library(poses_file)["right"]["TUBE"]
    assert stored["joints"] and stored["pose"]["x"] == pytest.approx(200.0)
    assert stored["note"] == "on the tube" and stored["saved_at"]


def test_reloading_the_storage_layer_reads_back_identically(client, poses_file):
    """R-WP-8: save, restart the storage layer, read back. `core.teach_poses` is a fresh
    read off disk with no shared state, which is what a backend restart amounts to."""
    from core import teach_poses

    client.post("/api/arms/right/poses", json={"name": "LIQUID_HANDLER_DECK"})
    client.post("/api/arms/left/poses", json={"name": "CAP_STORE"})

    reloaded = teach_poses.load(str(poses_file))
    assert set(reloaded) == {"right", "left"}

    right = waypoints.resolve("right", "LIQUID_HANDLER_DECK", path=str(poses_file))
    api_copy = client.get("/api/arms/right/poses").json()[0]
    assert right.joints == api_copy["joints"]
    assert right.xyz_rpy[0] == pytest.approx(api_copy["pose"]["x"])
    assert right.saved_at == api_copy["saved_at"]
    assert waypoints.resolve("left", "CAP_STORE", path=str(poses_file)).device == "left"


def test_a_second_backend_sees_what_the_first_one_taught(client, poses_file, monkeypatch):
    """The restart, end to end: a new app instance over the same file."""
    client.post("/api/arms/right/poses", json={"name": "APPROACH_RACK"})

    from backend.app.main import app

    with TestClient(app) as second:
        body = second.get("/api/arms/right/waypoints").json()
        rows = {r["name"]: r for r in body["waypoints"]}
        assert body["taught"] == 1 and rows["APPROACH_RACK"]["taught"]


def test_deletion_persists(client, poses_file):
    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    client.post("/api/arms/right/poses", json={"name": "APPROACH_RACK"})
    assert client.delete("/api/arms/right/poses/TUBE").status_code == 200

    assert list(library(poses_file)["right"]) == ["APPROACH_RACK"]
    with TestClient(_app()) as second:
        rows = {r["name"]: r for r in
                second.get("/api/arms/right/waypoints").json()["waypoints"]}
        assert rows["APPROACH_RACK"]["taught"] and not rows["TUBE"]["taught"]


def test_a_crash_between_the_temp_write_and_the_rename_leaves_the_library_intact(
        client, poses_file, monkeypatch):
    """R-WP-7. The write is temp-file + `os.replace`, so the whole failure window sits on
    the temp file: the library is either the old version or the new one, never a mixture.

    `_write_poses` is exercised directly rather than through the endpoint — a crash is a
    process death, and raising one through the ASGI stack tears down the test client
    instead of the thing under test.
    """
    from backend.app.api import teach

    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    before = poses_file.read_text()

    store = json.loads(before)
    store["right"]["APPROACH_RACK"] = dict(store["right"]["TUBE"], name="APPROACH_RACK")

    def power_cut(src, dst):
        raise OSError("power cut between the temp write and the rename")

    real_replace = teach.os.replace
    monkeypatch.setattr(teach.os, "replace", power_cut)
    with pytest.raises(OSError):
        teach._write_poses(store)

    # Byte-identical, still valid JSON, and the earlier waypoint still resolves.
    assert poses_file.read_text() == before
    assert waypoints.resolve("right", "TUBE", path=str(poses_file)).name == "TUBE"
    assert client.get("/api/arms/right/waypoints").json()["taught"] == 1

    # Restore only `os.replace` — never `monkeypatch.undo()`, which would also undo the
    # pose-file isolation and point the next write at the bench's real library.
    monkeypatch.setattr(teach.os, "replace", real_replace)
    teach._write_poses(store)
    assert set(library(poses_file)["right"]) == {"TUBE", "APPROACH_RACK"}


def test_a_crash_partway_through_serializing_leaves_the_library_intact(
        client, poses_file, monkeypatch):
    """The other half of the window: dying while the JSON is still being written. The
    half-written bytes land in the temp file, which no reader ever opens."""
    from backend.app.api import teach

    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    before = poses_file.read_text()

    def half_write(store, fh, **kwargs):
        fh.write('{"right": {"TU')
        raise OSError("disk full halfway through")

    monkeypatch.setattr(teach.json, "dump", half_write)
    with pytest.raises(OSError):
        teach._write_poses(json.loads(before))

    assert poses_file.read_text() == before
    assert client.get("/api/arms/right/waypoints").json()["taught"] == 1


def test_a_partially_written_temp_file_is_never_read_as_the_library(client, poses_file):
    """The temp file is a sibling (`<path>.tmp`); readers must not pick it up."""
    from core import teach_poses

    client.post("/api/arms/right/poses", json={"name": "TUBE"})
    (poses_file.parent / (poses_file.name + ".tmp")).write_text('{"right": {"TU')

    assert set(teach_poses.load(str(poses_file))["right"]) == {"TUBE"}
    assert client.get("/api/arms/right/waypoints").json()["taught"] == 1


def _app():
    from backend.app.main import app

    return app
