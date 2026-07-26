"""HZ_CAMERA_HOST rewrites the fleet's camera slots to the `remote` driver.

Settings.load() is exercised rather than the private helper: the switch has to win over
the per-device CAM_* overrides *and* over a whole HZ_FLEET_FILE, and load order is the
only thing that decides that.
"""
from __future__ import annotations

import json

import pytest

from core.config import CAM_ENV, CAMERA_TYPES, Settings

HOST = "http://bench.local:8100"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neutralize the bench .env.

    Settings.load() calls load_dotenv, and this repo ships a .env that pins real
    cameras — including CAM_GRIPPER_TYPE=still. Deleting the variables is not enough,
    because dotenv would then supply them; they have to be *present and empty*, which
    dotenv leaves alone (it never overrides an existing variable) and which
    core.config treats as "not set".
    """
    for var in ("HZ_CAMERA_HOST", "HZ_FLEET_FILE"):
        monkeypatch.delenv(var, raising=False)
    # Real hardware, explicitly. Simulation is the default now (D29), and a simulated slot
    # is rewritten to a mock type — which is exactly what this file asserts it is not.
    monkeypatch.setenv("HZ_SIM", "none")
    # Derived from CAM_ENV, not a hardcoded slot list: adding a camera to the fleet used to
    # leave its var un-neutralized here, so the bench .env leaked into the test and it
    # failed for a reason that had nothing to do with what it checks.
    for var in CAM_ENV.values():
        monkeypatch.setenv(var, "")
        monkeypatch.setenv(f"{var}_TYPE", "")
    for var in ("CAM_WIDTH", "CAM_HEIGHT", "CAM_FPS"):
        monkeypatch.setenv(var, "")


def cameras(fleet):
    return [e for e in fleet if e["id"].endswith("_cam")]


def test_unset_leaves_local_hardware_alone():
    s = Settings.load()
    assert s.camera_host == ""
    # A *local* driver type, whichever one the bench currently uses — `avf` today, since the
    # slots are pinned by AVFoundation uniqueID (core/cameras.py). Asserting the exact type
    # would make this test fail for every legitimate driver change, which is not what it is
    # about: the point is that nothing became `remote`.
    kinds = {e["type"] for e in cameras(s.fleet)}
    assert kinds and kinds <= set(CAMERA_TYPES)
    assert "remote" not in kinds


def test_every_camera_slot_becomes_remote(monkeypatch):
    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    s = Settings.load()
    assert s.camera_host == HOST
    slots = cameras(s.fleet)
    assert len(slots) == len(CAM_ENV)      # every configured slot, not a fixed count
    for entry in slots:
        assert entry["type"] == "remote"
        assert entry["base_url"] == HOST
        assert entry["remote_id"] == entry["id"]
        assert entry["name"]                      # the human label survives


def test_arms_and_liquid_handler_are_untouched(monkeypatch):
    """Borrowing frames from a bench does not mean sharing its hardware."""
    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    s = Settings.load()
    others = [e for e in s.fleet if not e["id"].endswith("_cam")]
    assert {e["type"] for e in others} == {"xarm", "opentrons"}
    assert all("base_url" not in e for e in others)


def test_local_addressing_is_dropped(monkeypatch):
    """A leftover serial or USB index on a slot that opens no device reads as config
    that is still in force — the next person debugging would chase it."""
    monkeypatch.setenv("CAM_OVERVIEW", "1")
    monkeypatch.setenv("CAM_OVERVIEW_TYPE", "camera")
    monkeypatch.setenv("CAM_GRIPPER", "SERIAL123")
    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    s = Settings.load()
    for entry in cameras(s.fleet):
        assert "source" not in entry and "serial" not in entry


def test_stream_format_survives(monkeypatch):
    """Width/height/fps stay as a record of what the viewpoint is expected to deliver;
    the remote decides the real format, and the UI shows both."""
    monkeypatch.setenv("CAM_WIDTH", "848")
    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    s = Settings.load()
    assert all(e["width"] == 848 for e in cameras(s.fleet))


def test_it_wins_over_a_whole_fleet_file(monkeypatch, tmp_path):
    """The point of a global switch: a clone can borrow the bench's eyes without
    editing the fleet it was handed."""
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps([
        {"type": "realsense", "id": "overview_cam", "name": "Overview", "serial": "X1"},
        {"type": "xarm", "id": "left", "name": "Left arm", "ip": "10.0.0.5"},
    ]))
    monkeypatch.setenv("HZ_FLEET_FILE", str(path))
    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    s = Settings.load()
    overview = next(e for e in s.fleet if e["id"] == "overview_cam")
    assert overview["type"] == "remote" and overview["base_url"] == HOST
    assert next(e for e in s.fleet if e["id"] == "left")["ip"] == "10.0.0.5"


@pytest.mark.parametrize("given", ["bench.local:8100", "http://bench.local:8100/",
                                   "  http://bench.local:8100  "])
def test_host_forms_normalize(monkeypatch, given):
    monkeypatch.setenv("HZ_CAMERA_HOST", given)
    assert Settings.load().camera_host == HOST


def test_slots_build_into_real_drivers(monkeypatch):
    """The rewritten entries must be something build_driver accepts — a config that only
    looks right would fail at boot, one device at a time, inside a caught exception."""
    from drivers import build_driver

    monkeypatch.setenv("HZ_CAMERA_HOST", HOST)
    for entry in cameras(Settings.load().fleet):
        driver = build_driver(entry)
        assert driver.stream_url == f"{HOST}/api/cameras/{entry['id']}/stream"
