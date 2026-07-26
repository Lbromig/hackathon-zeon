"""The engine's view of the device manager, and why the workflow needs joint replay."""
from __future__ import annotations

import json
import math

import pytest

from backend.app.services.device_access import DeviceGateway, WrongDeviceKind
from core import waypoints



class _StubManager:
    def __init__(self, drivers):
        self._drivers = {d.device_id: d for d in drivers}

    def get(self, device_id):
        return self._drivers[device_id]

    def all(self):
        return list(self._drivers.values())


def _fleet():
    from drivers.mock import (MockArmDriver, MockCameraDriver,
                              MockLiquidHandlerDriver)
    return _StubManager([
        MockArmDriver("right"), MockArmDriver("left"),
        MockLiquidHandlerDriver("ot"), MockCameraDriver("handover_cam"),
    ])


def test_require_arm_returns_the_arm():
    gw = DeviceGateway(_fleet())
    assert gw.require_arm("right").device_id == "right"


def test_pointing_an_arm_action_at_a_camera_fails_with_one_clear_error():
    """A plan naming the wrong kind of device is a plan bug.

    It must surface as that, at the top of the handler, rather than as an AttributeError
    from somewhere inside a motion call where the cause is unrecoverable from the traceback.
    """
    gw = DeviceGateway(_fleet())
    with pytest.raises(WrongDeviceKind) as exc:
        gw.require_arm("handover_cam")
    assert "camera" in str(exc.value) and "not an arm" in str(exc.value)


def test_require_liquid_handler_rejects_an_arm():
    gw = DeviceGateway(_fleet())
    with pytest.raises(WrongDeviceKind):
        gw.require_liquid_handler("right")


def test_a_device_absent_from_the_fleet_raises_key_error_not_wrong_kind():
    """Absent and wrong-kind are different problems and must not be conflated."""
    gw = DeviceGateway(_fleet())
    with pytest.raises(KeyError):
        gw.require_arm("nonexistent")


def test_simulation_state_comes_from_config_not_from_sniffing_the_driver():
    """D25: `info.vendor == "mock"` is a string comparison standing in for a config fact.

    Real drivers carry no marker to sniff, and a pure-computation action touches no device
    at all — so simulation has to be told, not guessed.
    """
    gw = DeviceGateway(_fleet(), simulated={"right": True, "left": False})
    assert gw.is_simulated("right") is True
    assert gw.is_simulated("left") is False
    assert gw.is_simulated("ot") is False, "unknown defaults to not-simulated"


def test_all_simulated_reports_every_device_for_the_run_banner():
    gw = DeviceGateway(_fleet(), simulated={"right": True})
    reported = gw.all_simulated()
    assert set(reported) == {"right", "left", "ot", "handover_cam"}
    assert reported["right"] is True


# --- why the workflow cannot use absolute cartesian moves ---------------------

#: The hand-motion cap this workflow does not fit inside, in mm. A literal rather than a
#: read of ``TEACH_LIMITS["max_move_to_jump"]``, which the operator raised to effectively
#: unlimited on 2026-07-26 precisely because it refused an ordinary 563 mm traverse between
#: two taught points. The number here records *why* joint replay is required, so the test
#: keeps its meaning whatever the live cap is set to.
HISTORIC_HAND_MOTION_CAP_MM = 250.0


def test_the_workflow_has_moves_far_longer_than_a_hand_motion_cap():
    """Measured on the taught library: four inter-waypoint moves exceed 250 mm.

    A `max_move_to_jump` cap guards **hand-driven** motion — it stops a typo in a typed
    absolute pose from flinging the arm across the bench. It is not a statement that a
    400 mm move is unsafe: these waypoints were taught by physically walking the arm to
    each, so both configurations are reachable and the path between them was traversed by
    hand.

    The consequence, and the reason this test exists: replaying a taught waypoint must go
    through **joint space**, not an absolute cartesian move. Joint replay reproduces the
    configuration that was physically reached, so there is no IK branch to guess at and no
    hand-motion cap to inherit. If anyone ever routes waypoint replay through the teach
    API's absolute-move path, this workflow stops working at step 3 — and raising the cap to
    hide that, as was done for the teach UI, would remove the guard rather than fix the path.
    """
    lib = json.load(open("data/teach_poses.json"))

    def xyz(device, name):
        p = lib[device][name]["pose"]
        return (p["x"], p["y"], p["z"])

    # Consecutive same-arm pairs the workflow actually commands.
    pairs = [
        ("right", "APPROACH_RACK", "APPROACH_TUBE_GRAB"),
        ("left", "APPROACH_CAP_STORE", "CAP_STORE"),
        ("right", "APPROACH_TUBE_TRANSFER", "TRANSITION_ROBOT_TABLE"),
        ("right", "TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE"),
    ]
    for device, a, b in pairs:
        if a not in lib.get(device, {}) or b not in lib.get(device, {}):
            pytest.skip(f"{device}/{a}->{b} not taught yet")
        assert math.dist(xyz(device, a), xyz(device, b)) > HISTORIC_HAND_MOTION_CAP_MM, (
            f"{device}/{a}->{b} was expected to exceed the "
            f"{HISTORIC_HAND_MOTION_CAP_MM:g} mm hand-motion cap"
        )


def test_every_taught_workflow_waypoint_has_joints_recorded():
    """Joint replay is only available if the joints were captured.

    A waypoint with a cartesian pose but no joints falls back to an absolute move, which is
    the path that inherits the hand-motion cap and the IK ambiguity. For the long traverses
    above that fallback would refuse to run, so a missing `joints` array is a real defect in
    the taught library rather than a cosmetic gap.
    """
    lib = json.load(open("data/teach_poses.json"))
    missing = []
    for device, name in [(s.device, s.name) for s in waypoints.SPEC]:
        entry = lib.get(device, {}).get(name)
        if entry is None:
            continue                      # untaught is a different test's problem
        if not entry.get("joints"):
            missing.append(f"{device}/{name}")
    assert not missing, (
        "taught but with no joint angles, so replay would fall back to an absolute "
        f"cartesian move: {missing}. Re-teach these."
    )
