"""`GET /api/logs` (R-LOG-4). Polled with a cursor; no websocket (S10)."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.obs import log as obs


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A backend whose log file is a temp file, with the real handlers installed on it."""
    path = tmp_path / "zeon.jsonl"
    monkeypatch.setattr(settings, "log_file", str(path))
    monkeypatch.setenv("HZ_STARTUP_SNAPSHOT", "0")

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    obs.configure(path=str(path), level="DEBUG", console=False, force=True)

    from backend.app.main import app

    with TestClient(app) as c:
        yield c

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    obs.clear()


def emit(**context):
    """Log one record the way the action wrapper will."""
    with obs.action_context(**context):
        logging.getLogger("engine.handlers.arm").info(
            "moved", extra={"event": "action_output", "outputs": {"waypoint": "HOME"}})
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_the_newest_page_comes_back_without_a_cursor(client):
    emit(run_id="r_1", aid=1, index=0, action_kind="arm.waypoint", device="right")
    body = client.get("/api/logs", params={"limit": 50}).json()
    assert body["records"], "the log read path returned nothing the writer had written"
    assert body["cursor"]
    assert body["reset"] is False
    last = body["records"][-1]
    assert last["msg"] == "moved" and last["run_id"] == "r_1"


def test_a_cursor_returns_only_what_is_new(client):
    emit(run_id="r_1", aid=1)
    first = client.get("/api/logs").json()
    emit(run_id="r_1", aid=2)
    second = client.get("/api/logs", params={"cursor": first["cursor"]}).json()
    assert [r["aid"] for r in second["records"]] == [2]
    third = client.get("/api/logs", params={"cursor": second["cursor"]}).json()
    assert third["records"] == []


def test_filtering_by_aid_is_the_per_action_drill_down(client):
    """R-UI-8. This is the query that returns nothing at all if D23 is got wrong."""
    emit(run_id="r_1", aid=1, action_kind="arm.waypoint", device="right")
    emit(run_id="r_1", aid=2, action_kind="camera.snapshot", device="handover_cam")
    body = client.get("/api/logs", params={"run_id": "r_1", "aid": 2}).json()
    assert [r["aid"] for r in body["records"]] == [2]
    assert body["records"][0]["device"] == "handover_cam"


def test_the_typed_outputs_survive_the_round_trip(client):
    """A record's structure has to reach the browser as structure, not as a repr."""
    emit(run_id="r_1", aid=7)
    body = client.get("/api/logs", params={"aid": 7}).json()
    assert body["records"][0]["outputs"] == {"waypoint": "HOME"}
    assert body["records"][0]["event"] == "action_output"


def test_filters_compose(client):
    emit(run_id="r_1", aid=1, device="right")
    emit(run_id="r_2", aid=1, device="left")
    body = client.get("/api/logs", params={"run_id": "r_2", "device": "left"}).json()
    assert len(body["records"]) == 1 and body["records"][0]["run_id"] == "r_2"


def test_level_filters_from_a_floor_upwards(client):
    logging.getLogger("x").debug("noise")
    logging.getLogger("x").error("real problem")
    for handler in logging.getLogger().handlers:
        handler.flush()
    body = client.get("/api/logs", params={"level": "warning"}).json()
    assert [r["msg"] for r in body["records"]] == ["real problem"]


def test_a_missing_log_file_is_an_empty_page_not_a_500(client, monkeypatch, tmp_path):
    """The Logs tab must render before anything has been logged."""
    monkeypatch.setattr(settings, "log_file", str(tmp_path / "not-written-yet.jsonl"))
    r = client.get("/api/logs")
    assert r.status_code == 200 and r.json()["records"] == []


def test_the_limit_is_bounded(client):
    """One request must not be able to ask the server to parse the whole file."""
    assert client.get("/api/logs", params={"limit": 100000}).status_code == 422
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422


def test_rotation_is_reported_to_the_client(client, tmp_path):
    """The UI must be able to say "log rotated" rather than show a silent gap."""
    import os

    emit(run_id="r_1", aid=1)
    cursor = client.get("/api/logs").json()["cursor"]

    path = settings.log_file
    os.rename(path, path + ".1")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 99, "level": "INFO", "msg": "after rotation"}) + "\n")

    body = client.get("/api/logs", params={"cursor": cursor}).json()
    assert body["reset"] is True
    assert [r["msg"] for r in body["records"]] == ["after rotation"]


def test_there_is_no_log_websocket():
    """S10: a polled GET with a cursor is the whole transport. A follower socket would be
    ~150 lines whose failure mode is invisible until you need the log."""
    from backend.app.main import app

    ws_paths = [r.path for r in app.routes if r.__class__.__name__ == "APIWebSocketRoute"]
    assert not [p for p in ws_paths if "log" in p]
