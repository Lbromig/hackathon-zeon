"""Camera identity is hardcoded in core/cameras.py and cannot be overridden by an index.

The bug these guard against is not hypothetical. Every camera slot used to be addressed by
an OpenCV device index from .env, and on macOS an index is not an identity: it renumbers on
any replug, and measured 2026-07-26, between two enumerations seconds apart with nothing
touched (the built-in MacBook camera moved from index 2 to index 0). A viewpoint could
therefore come up aimed at a different camera — or at the operator — while still reporting
healthy, which is the one failure mode a verification rig cannot tolerate.
"""
from __future__ import annotations

import pytest

from core.cameras import BENCH_CAMERAS, BY_SLOT
from core.config import CAM_ENV, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neutralize the bench .env — see test_camera_host_config for why empty, not deleted."""
    for var in ("HZ_CAMERA_HOST", "HZ_FLEET_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HZ_SIM", "none")
    for var in CAM_ENV.values():
        monkeypatch.setenv(var, "")
        monkeypatch.setenv(f"{var}_TYPE", "")


def _cameras(fleet):
    return {e["id"]: e for e in fleet if e.get("id") in BY_SLOT}


def test_every_bench_camera_is_pinned_by_unique_id():
    cams = _cameras(Settings.load().fleet)
    assert set(cams) == set(BY_SLOT), "fleet must carry exactly the four bench viewpoints"
    for slot, entry in cams.items():
        assert entry["type"] == "avf", f"{slot} must use the identity-addressed driver"
        assert entry["unique_id"] == BY_SLOT[slot].unique_id
        assert entry["usb_serial"] == BY_SLOT[slot].usb_serial
        # An index would reintroduce the renumbering hazard through the back door.
        assert "source" not in entry, f"{slot} must not carry a device index"


def test_unique_ids_and_serials_are_distinct():
    """Two slots sharing a key would silently be the same camera under two names."""
    assert len({c.unique_id for c in BENCH_CAMERAS}) == len(BENCH_CAMERAS)
    assert len({c.usb_serial for c in BENCH_CAMERAS}) == len(BENCH_CAMERAS)


def test_unique_id_encodes_a_usb_location():
    """The key must be an AVFoundation uniqueID, whose high bytes are the USB locationID.

    An Apple camera's uniqueID is a UUID instead, so this also rejects ever pinning a slot
    to the built-in or Continuity camera.
    """
    for cam in BENCH_CAMERAS:
        assert cam.unique_id.startswith("0x"), cam.slot
        int(cam.unique_id, 16)                      # parses as hex
        assert cam.usb_serial.isdigit(), cam.slot


@pytest.mark.parametrize("slot", sorted(BY_SLOT))
def test_cam_slot_index_is_ignored_not_obeyed(monkeypatch, slot):
    """A leftover CAM_<SLOT>=<index> in someone's .env must not re-point a viewpoint."""
    monkeypatch.setenv(CAM_ENV[slot], "0")
    entry = _cameras(Settings.load().fleet)[slot]
    assert entry["type"] == "avf"
    assert entry["unique_id"] == BY_SLOT[slot].unique_id
    assert "source" not in entry


@pytest.mark.parametrize("slot", sorted(BY_SLOT))
def test_switch_to_index_driver_requires_an_explicit_index(monkeypatch, slot):
    """CAM_<SLOT>_TYPE=camera with no index would leave OpenCV's default of 0 in force.

    Index 0 is regularly the built-in MacBook camera, so the slot would stream the operator
    and still look healthy. The switch is refused instead.
    """
    monkeypatch.setenv(f"{CAM_ENV[slot]}_TYPE", "camera")
    entry = _cameras(Settings.load().fleet)[slot]
    assert entry["type"] == "avf", "must keep the pinned driver rather than default to 0"


@pytest.mark.parametrize("slot", sorted(BY_SLOT))
def test_explicit_type_and_index_together_are_honoured(monkeypatch, slot):
    """The escape hatch still works: hardware away -> replay, or a deliberate index."""
    monkeypatch.setenv(f"{CAM_ENV[slot]}_TYPE", "camera")
    monkeypatch.setenv(CAM_ENV[slot], "7")
    entry = _cameras(Settings.load().fleet)[slot]
    assert entry["type"] == "camera"
    assert entry["source"] == 7
    # The port/unit keys belong to the avf driver; leaving them would imply the slot is
    # still pinned to a physical unit when it is now just an index.
    assert "unique_id" not in entry
    assert "usb_serial" not in entry


def test_simulation_still_replaces_camera_slots(monkeypatch):
    """Hardcoding identities must not defeat the simulated default (D29)."""
    monkeypatch.setenv("HZ_SIM", "all")
    for entry in _cameras(Settings.load().fleet).values():
        assert entry["type"] == "mock_tag_camera"
