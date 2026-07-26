"""Simulation config and the per-camera identity/resolution schema (D25, D29, R-CAM-6…13).

The load order is the whole substance here: simulation is applied last, so it wins over the
per-device env vars, a whole `HZ_FLEET_FILE` and `HZ_CAMERA_HOST`. A device that was
simulated *and* still held a real IP or USB index would be a configuration that reads as
in force while doing something else.
"""
from __future__ import annotations

import json

import pytest

from core.config import (CAM_ENV, CAMERA_MODES, DEFAULT_CAMERA_MODE, DEFAULT_SERVO_CAMERAS,
                         SIM_SUBSTITUTIONS, CameraIdentity, Settings, mode_for)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neutralize the bench `.env`, which pins real cameras and one `still` slot.

    Deleting is not enough — dotenv would then supply them. They have to be present and
    empty, which dotenv leaves alone and `core.config` treats as unset.
    """
    for var in ("HZ_CAMERA_HOST", "HZ_FLEET_FILE", "HZ_SIM", "HZ_SIM_SESSION",
                "HZ_SERVO_CAMERAS", "HZ_LOG_FILE"):
        monkeypatch.delenv(var, raising=False)
    for var in CAM_ENV.values():
        monkeypatch.setenv(var, "")
        monkeypatch.setenv(f"{var}_TYPE", "")
    for var in ("CAM_WIDTH", "CAM_HEIGHT", "CAM_FPS"):
        monkeypatch.setenv(var, "")


def types_by_id(fleet) -> dict[str, str]:
    return {e["id"]: e["type"] for e in fleet}


# --- the default (D29 / Q8) ---------------------------------------------------

def test_simulation_is_the_default():
    """A fresh clone with no configuration must run simulated — that is the demo path."""
    s = Settings.load()
    assert s.sim.all_devices and s.sim.any
    assert s.sim.device_ids == {e["id"] for e in s.fleet}
    # Substitution, not a flag: every entry now names a mock driver type.
    for did, kind in types_by_id(s.fleet).items():
        assert kind in SIM_SUBSTITUTIONS.values(), f"{did} is still {kind!r}"


def test_real_hardware_is_an_explicit_opt_in():
    s = _load(HZ_SIM="none")
    assert not s.sim.any
    assert types_by_id(s.fleet)["left"] == "xarm"
    assert s.is_simulated("left") is False


def test_simulation_is_selectable_per_device_class(monkeypatch):
    """R-SIM-3: real arms with simulated cameras, and the reverse."""
    s = _load(HZ_SIM="cameras")
    kinds = types_by_id(s.fleet)
    assert kinds["left"] == "xarm" and kinds["ot"] == "opentrons"
    assert kinds["handover_cam"] == SIM_SUBSTITUTIONS["realsense"]
    assert s.is_simulated("handover_cam") and not s.is_simulated("left")


def test_a_single_device_can_be_named():
    s = _load(HZ_SIM="left,lh")
    kinds = types_by_id(s.fleet)
    assert kinds["left"] == "mock_arm" and kinds["right"] == "xarm"
    assert kinds["ot"] == "mock_liquid_handler"


def test_an_unknown_sim_token_warns_and_is_ignored(caplog):
    """It must not stop the boot, and it must not silently leave a device real."""
    with caplog.at_level("WARNING"):
        s = _load(HZ_SIM="left,not_a_device")
    assert s.sim.device_ids == {"left"}
    assert any("not_a_device" in r.getMessage() for r in caplog.records)


# --- D25: `simulated` comes from config, never from a driver ------------------

def test_a_pure_compute_action_reports_the_runs_reality():
    """D25/R-SIM-6: an action with no device touches nothing, so "no device was mocked"
    must not be read as "this was real"."""
    assert Settings.load().is_simulated(None) is True          # default run is simulated
    assert _load(HZ_SIM="none").is_simulated(None) is False


def test_an_unknown_device_is_not_simulated():
    """Absent from the resolved set means not simulated — never a default of True, which
    would let a real device be reported as fake."""
    assert Settings.load().is_simulated("no_such_device") is False


# --- load order ---------------------------------------------------------------

def test_simulation_wins_over_a_whole_fleet_file(tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps([{"type": "xarm", "id": "solo", "ip": "10.0.0.1"}]))
    s = _load(HZ_FLEET_FILE=str(path), HZ_SIM="all")
    assert types_by_id(s.fleet) == {"solo": "mock_arm"}
    # The address is left on the entry but the driver behind it is a mock, so it cannot
    # be dialled. Substitution is the mechanism (D2), not address scrubbing.
    assert s.fleet[0]["ip"] == "10.0.0.1"


def test_camera_host_keeps_the_camera_slots_real_by_default():
    """HZ_CAMERA_HOST is an explicit statement about where frames come from. Simulating
    them by default would make the setting a no-op while looking like it was in force."""
    s = _load(HZ_CAMERA_HOST="http://bench.local:8100")
    kinds = types_by_id(s.fleet)
    assert kinds["handover_cam"] == "remote"
    assert kinds["left"] == "mock_arm"                # everything else still simulated
    assert not s.is_simulated("handover_cam")


def test_an_explicit_hz_sim_still_overrides_camera_host():
    s = _load(HZ_CAMERA_HOST="http://bench.local:8100", HZ_SIM="all")
    assert types_by_id(s.fleet)["handover_cam"] == SIM_SUBSTITUTIONS["remote"]


# --- camera resolution schema (R-CAM-10…13) ----------------------------------

def test_the_four_offered_modes_are_all_30fps_and_720p_is_the_default():
    assert [m.key for m in CAMERA_MODES] == [
        "640x480@30", "848x480@30", "1280x720@30", "1920x1080@30"]
    assert DEFAULT_CAMERA_MODE.key == "1280x720@30"


def test_640x480_is_labelled_below_the_servo_minimum():
    """P-1 is a measurement, not a preference: the tube tag is undetectable at 640x480 and
    detectable in the same frame upscaled 2x. A UI that offers it unlabelled is a defect."""
    low = mode_for(640, 480)
    assert low is not None and low.servo_capable is False
    assert all(m.servo_capable for m in CAMERA_MODES if m is not low)


def test_an_unoffered_mode_is_not_silently_snapped_to_a_neighbour(caplog):
    """OpenCV substitutes the nearest format silently; that is the bug R-CAM-12 addresses.
    Config must not perform the same substitution before the device even sees it."""
    assert mode_for(1024, 768) is None
    with caplog.at_level("WARNING"):
        s = _load(CAM_WIDTH="1024", CAM_HEIGHT="768")
    slot = s.camera("handover_cam")
    assert (slot.mode.width, slot.mode.height) == (1024, 768)
    assert slot.mode.servo_capable is False
    assert any("not an offered mode" in r.getMessage() for r in caplog.records)


def test_every_camera_slot_gets_a_config_defaulting_to_720p():
    s = Settings.load()
    assert set(s.cameras) == set(CAM_ENV)
    for slot in s.cameras.values():
        assert slot.mode == DEFAULT_CAMERA_MODE
        assert slot.stream == "color"                  # R-CAM-14: colour is the default


def test_per_camera_resolution_override():
    s = _load(CAM_WIDTH_HANDOVER_CAM="1920", CAM_HEIGHT_HANDOVER_CAM="1080")
    assert s.camera("handover_cam").mode.key == "1920x1080@30"
    assert s.camera("overview_cam").mode == DEFAULT_CAMERA_MODE


# --- camera identity (R-CAM-6/7) ---------------------------------------------

def test_an_index_bound_slot_has_no_identity():
    """The empty identity is the meaningful case: it says "bound by index alone", which
    R-CAM-6 forbids for anything the offset solve depends on."""
    assert CameraIdentity().is_bound is False
    for route in ("sdk_serial", "unique_id", "usb_serial", "reference_frame"):
        assert CameraIdentity(**{route: "x"}).is_bound is True


def test_the_sdk_serial_and_the_usb_serial_are_separate_fields():
    """They are different values for the same unit (documented in
    docs/v2/NOTES_camera_identity.md). One field for both would invent a mapping, and
    pinning a camera by the USB serial binds nothing, silently."""
    ident = CameraIdentity(sdk_serial="125123020017", usb_serial="A1B2C3")
    assert ident.sdk_serial != ident.usb_serial


def test_every_bench_slot_carries_a_verifiable_identity():
    """The bench's four units are pinned by AVFoundation uniqueID in `core/cameras.py`, so
    no slot is bound by index alone — which is what R-CAM-6 requires."""
    s = _load(HZ_SIM="none")
    assert set(s.cameras) == set(CAM_ENV)
    for slot in s.cameras.values():
        assert slot.identity.is_bound, f"{slot.id} has no identity beyond an index"
        assert slot.identity.unique_id and slot.identity.usb_serial


def test_a_slots_identity_survives_being_simulated():
    """A simulated camera stands in *for a slot*. The slot's identity is configuration, so
    it must still be reported — otherwise the frontend cannot show identity (R-CAM-8) in
    the mode the system now boots in by default."""
    s = Settings.load()
    assert s.is_simulated("handover_cam")
    assert s.camera("handover_cam").identity.is_bound


# --- servo cameras come from config (D19) ------------------------------------

def test_servo_cameras_default_to_the_measured_pair():
    s = Settings.load()
    assert s.servo_cameras == DEFAULT_SERVO_CAMERAS
    assert [cid for cid, slot in s.cameras.items() if slot.servo] == list(DEFAULT_SERVO_CAMERAS)


def test_the_camera_the_operator_calls_the_gripper_cam_is_not_a_default_servo_view():
    """GAP_ANALYSIS §3.1: the slot named `gripper_cam` is aimed across the room and
    detected zero tags in 53 frames; `gripper_left_cam` is physically the right arm's."""
    assert "gripper_cam" not in DEFAULT_SERVO_CAMERAS
    assert "gripper_left_cam" in DEFAULT_SERVO_CAMERAS


def test_servo_cameras_are_overridable_without_a_code_change():
    s = _load(HZ_SERVO_CAMERAS="overview_cam, gripper_cam")
    assert s.servo_cameras == ("overview_cam", "gripper_cam")
    assert s.camera("overview_cam").servo and not s.camera("handover_cam").servo


def test_a_servo_camera_missing_from_the_fleet_warns(caplog):
    with caplog.at_level("WARNING"):
        s = _load(HZ_SERVO_CAMERAS="ghost_cam")
    assert "ghost_cam" not in s.cameras
    assert any("ghost_cam" in r.getMessage() for r in caplog.records)


# --- observability paths -----------------------------------------------------

def test_log_and_artifact_paths_are_single_and_overridable(tmp_path):
    s = Settings.load()
    assert s.log_file.endswith(".jsonl")             # one file, structured (R-LOG-2/3)
    assert s.log_backup_count and s.log_max_bytes    # bounded (R-LOG-7)
    moved = _load(HZ_LOG_FILE=str(tmp_path / "x.jsonl"),
                  HZ_ARTIFACT_DIR=str(tmp_path / "runs"))
    assert moved.log_file == str(tmp_path / "x.jsonl")
    assert moved.artifact_dir == str(tmp_path / "runs")


def _load(**env: str) -> Settings:
    """Settings.load() with env vars set for the duration of one call."""
    import os

    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Settings.load()
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
