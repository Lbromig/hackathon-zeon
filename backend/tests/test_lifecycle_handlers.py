"""Lifecycle handlers: initialize and reconnect (R-INIT-1/2/4/7, R-ARM-7).

Two properties carry almost every test here, and both are about *not* stopping:

* **One device failing must not stop the others** (R-INIT-1). A camera that cannot be
  opened must not leave both arms un-enabled, and the failure must be in the record rather
  than only in a traceback.
* **An arm with no taught HOME is warned about and skipped** (R-INIT-4). Not an error, and
  above all not a guessed home — the two arms share a table, so "roughly home" is a
  collision. The warning has to be reachable by code, because the readiness panel matching
  on message text is how that quietly stops working.
"""
from __future__ import annotations

import json
import logging
import threading

import pytest

import drivers.mock  # noqa: F401  -- registers the mock drivers
from backend.app.engine import actions, blackboard, context
from backend.app.engine.handlers import lifecycle
from core import waypoints
from core.config import settings
from drivers import build_driver
from drivers.base import ConnectionState, DeviceInfo, InstrumentDriver, InstrumentKind
from drivers.capabilities.arm import ArmDriver


# --- fixtures ----------------------------------------------------------------------

def entry(*, joints=(0.0, 10.0, 20.0, 30.0, 40.0, 50.0)) -> dict:
    return {
        "name": "HOME",
        "pose": {"x": 200.0, "y": 0.0, "z": 300.0, "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
        "joints": list(joints) if joints else None,
        "gripper_width": None, "note": "", "saved_at": "2026-07-26T09:00:00+00:00",
    }


def make_arm(device_id: str) -> ArmDriver:
    return build_driver({"type": "mock_arm", "id": device_id})


class StubCamera(InstrumentDriver):
    """A camera that counts captures, so the settle-frame discard is observable."""
    kind = InstrumentKind.CAMERA

    def __init__(self, device_id: str, *, fail: bool = False) -> None:
        super().__init__(device_id, {})
        self.captures = 0
        self.jpegs = 0
        self._fail = fail

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.device_id, kind=InstrumentKind.CAMERA)

    def status(self) -> dict:
        return {"connected": self._state == ConnectionState.CONNECTED}

    def connect(self) -> None:
        if self._fail:
            raise RuntimeError("device or resource busy")
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def capture(self):
        self.captures += 1
        return [[0, 0], [0, 0]]

    def capture_jpeg(self, quality: int = 85) -> bytes:
        self.jpegs += 1
        return b"\xff\xd8\xff\xd9"


class StubLiquidHandler(InstrumentDriver):
    kind = InstrumentKind.LIQUID_HANDLER

    def __init__(self, device_id: str, *, with_initialize: bool) -> None:
        super().__init__(device_id, {})
        self.initialized = 0
        if with_initialize:
            self.initialize = self._initialize      # type: ignore[method-assign]

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.device_id,
                          kind=InstrumentKind.LIQUID_HANDLER)

    def status(self) -> dict:
        return {"connected": self._state == ConnectionState.CONNECTED}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def _initialize(self) -> None:
        self.initialized += 1


class Devices:
    def __init__(self, **drivers) -> None:
        self._drivers = dict(drivers)

    def get(self, device_id: str):
        if device_id not in self._drivers:
            raise KeyError(device_id)
        return self._drivers[device_id]

    def require_arm(self, device_id: str):
        driver = self.get(device_id)
        if not isinstance(driver, ArmDriver):
            raise TypeError(f"{device_id} is not an arm")
        return driver

    def require_liquid_handler(self, device_id: str):
        return self.get(device_id)

    def is_simulated(self, device_id: str) -> bool:
        return True


def make_ctx(action, devices, *, abort=None, pause=None,
             artifact_dir: str = "") -> context.ActionContext:
    ctx = context.ActionContext(
        run_id="test-run", action=action, devices=devices,
        blackboard=blackboard.Blackboard(),
        log=logging.getLogger("test.lifecycle"), simulated=True,
        artifact_dir=artifact_dir,
    )
    ctx._abort = abort
    ctx._pause = pause
    return ctx


@pytest.fixture
def fleet(monkeypatch):
    """A configured fleet the handler can enumerate. `initialize(device=None)` reads this
    rather than the live driver set, so a device whose driver failed to build still gets an
    outcome recorded (R-INIT-1) instead of vanishing from the report."""
    def _set(*ids: str) -> None:
        monkeypatch.setattr(settings, "fleet", [{"id": i, "type": "stub"} for i in ids])
    return _set


def outcome_for(out: actions.InitializeOutputs, device: str) -> dict:
    matches = [d for d in out.devices if d["device"] == device]
    assert matches, f"no outcome recorded for {device!r} in {out.devices}"
    return matches[0]


# --- registration -------------------------------------------------------------------

def test_both_lifecycle_kinds_have_handlers():
    for kind in ("lifecycle.initialize", "lifecycle.reconnect"):
        assert actions.handler_for(kind) is not None, kind


# --- initialize: enumeration and per-device outcomes (R-INIT-1/2) --------------------

def test_no_device_means_every_configured_device(fleet, isolated_teach_poses):
    fleet("left", "right", "cam", "ot")
    devices = Devices(left=make_arm("left"), right=make_arm("right"),
                      cam=StubCamera("cam"), ot=StubLiquidHandler("ot", with_initialize=True))
    action = actions.Initialize(home_after=False)

    out = lifecycle.initialize(action, make_ctx(action, devices))

    assert [d["device"] for d in out.devices] == ["left", "right", "cam", "ot"]
    assert all(d["connected"] for d in out.devices)


def test_one_named_device_initializes_only_that_one(fleet, isolated_teach_poses):
    fleet("left", "right")
    left, right = make_arm("left"), make_arm("right")
    action = actions.Initialize(device="left", home_after=False)

    out = lifecycle.initialize(action, make_ctx(action, Devices(left=left, right=right)))

    assert [d["device"] for d in out.devices] == ["left"]
    assert right.state != ConnectionState.CONNECTED, "the other arm was left alone"


def test_arms_get_their_faults_cleared_and_their_axes_enabled(fleet, isolated_teach_poses):
    fleet("left")

    class Recording(type(make_arm("left"))):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.order: list[str] = []

        def clear_errors(self):
            self.order.append("clear")

        def enable(self, on: bool = True):
            self.order.append(f"enable({on})")
            super().enable(on)

    arm = Recording("left", {"type": "mock_arm", "id": "left"})
    action = actions.Initialize(home_after=False)
    lifecycle.initialize(action, make_ctx(action, Devices(left=arm)))

    assert arm.order == ["clear", "enable(True)"], (
        "clear before enable: a latched fault refuses the enable, so the other order fails "
        "on precisely the arm that needed initializing")


def test_cameras_discard_the_settle_frames_before_keeping_one(fleet, isolated_teach_poses,
                                                              tmp_path):
    """The first frames off a UVC camera are auto-exposure settling, so "the first frame" is
    reliably the worst one the camera will ever produce."""
    fleet("cam")
    cam = StubCamera("cam")
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(cam=cam), artifact_dir=str(tmp_path))

    out = lifecycle.initialize(action, ctx)

    assert cam.captures == lifecycle.CAMERA_SETTLE_FRAMES + 1
    assert lifecycle.CAMERA_SETTLE_FRAMES >= 1
    assert outcome_for(out, "cam")["connected"] is True
    artifacts = ctx.collected_artifacts()
    assert len(artifacts) == 1 and artifacts[0].camera == "cam"
    assert (tmp_path / "init_cam.jpg").read_bytes() == b"\xff\xd8\xff\xd9"


def test_a_camera_still_counts_as_initialized_when_there_is_nowhere_to_store_the_frame(
        fleet, isolated_teach_poses):
    """Capturing is the check; storing is the record. A run with no artifact directory is
    not a failed initialization."""
    fleet("cam")
    cam = StubCamera("cam")
    action = actions.Initialize(home_after=False)
    out = lifecycle.initialize(action, make_ctx(action, Devices(cam=cam)))
    assert cam.captures == lifecycle.CAMERA_SETTLE_FRAMES + 1
    assert cam.jpegs == 0
    assert outcome_for(out, "cam")["connected"] is True
    assert "not stored" in outcome_for(out, "cam")["detail"]


def test_the_liquid_handler_delegates_to_its_own_initialize_routine(fleet,
                                                                   isolated_teach_poses):
    fleet("ot")
    ot = StubLiquidHandler("ot", with_initialize=True)
    action = actions.Initialize(home_after=False)
    out = lifecycle.initialize(action, make_ctx(action, Devices(ot=ot)))
    assert ot.initialized == 1
    assert "initialize routine ran" in outcome_for(out, "ot")["detail"]


def test_a_liquid_handler_with_no_initialize_routine_is_skipped_with_a_warning(
        fleet, isolated_teach_poses):
    """The axis wiggle and Z retract live on the driver (S4/D28) and may not exist yet.
    Missing must be a warning and a skip — an initialization that reports success while the
    head never moved is the bug class D6 exists for."""
    fleet("ot")
    ot = StubLiquidHandler("ot", with_initialize=False)
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(ot=ot))

    out = lifecycle.initialize(action, ctx)

    assert [w.code for w in ctx.collected_warnings()] == ["lh_initialize_unavailable"]
    assert outcome_for(out, "ot")["connected"] is True
    assert "skipped" in outcome_for(out, "ot")["detail"]


def test_the_simulated_flag_comes_from_the_resolved_config_not_the_driver(
        fleet, isolated_teach_poses):
    """D25. A vendor string is the wrong source in the dangerous direction: real drivers have
    no `live` key and reading it raises."""
    fleet("left")

    class HalfSim(Devices):
        def is_simulated(self, device_id: str) -> bool:
            return device_id == "left"

    action = actions.Initialize(home_after=False)
    out = lifecycle.initialize(action, make_ctx(action, HalfSim(left=make_arm("left"))))
    assert outcome_for(out, "left")["simulated"] is True


# --- initialize: one failure must not stop the rest (R-INIT-1) ----------------------

def test_one_device_failing_still_initializes_the_rest_and_records_the_failure(
        fleet, isolated_teach_poses):
    fleet("left", "cam", "right")
    left, right = make_arm("left"), make_arm("right")
    cam = StubCamera("cam", fail=True)
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(left=left, cam=cam, right=right))

    out = lifecycle.initialize(action, ctx)

    assert outcome_for(out, "cam")["connected"] is False
    assert "busy" in outcome_for(out, "cam")["detail"]
    assert outcome_for(out, "left")["connected"] is True
    assert outcome_for(out, "right")["connected"] is True, (
        "the device *after* the failure must still be initialized")
    assert left.state == ConnectionState.CONNECTED
    assert right.state == ConnectionState.CONNECTED
    assert "device_init_failed" in [w.code for w in ctx.collected_warnings()]


def test_a_device_whose_init_routine_fails_records_connected_and_not_initialized(
        fleet, isolated_teach_poses):
    """"Connected" on its own would read as ready."""
    fleet("left")

    class Unenableable(type(make_arm("left"))):
        def enable(self, on: bool = True):
            raise RuntimeError("servo error 1")

    arm = Unenableable("left", {"type": "mock_arm", "id": "left"})
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(left=arm))

    out = lifecycle.initialize(action, ctx)

    row = outcome_for(out, "left")
    assert row["connected"] is True
    assert "init failed" in row["detail"] and "servo error 1" in row["detail"]
    assert "device_init_failed" in [w.code for w in ctx.collected_warnings()]


def test_a_configured_device_with_no_driver_is_reported_rather_than_dropped(
        fleet, isolated_teach_poses):
    """R-INIT-1 says initialization *discovers* the configured devices. A driver that failed
    to build is exactly the outcome worth recording, so enumeration comes from config."""
    fleet("left", "ghost")
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(left=make_arm("left")))

    out = lifecycle.initialize(action, ctx)

    assert [d["device"] for d in out.devices] == ["left", "ghost"]
    assert outcome_for(out, "ghost")["connected"] is False
    assert "no driver" in outcome_for(out, "ghost")["detail"]
    assert "device_unavailable" in [w.code for w in ctx.collected_warnings()]


def test_initialize_is_pausable_between_devices(fleet, isolated_teach_poses):
    fleet("left", "right", "cam")
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(left=make_arm("left"), right=make_arm("right"),
                                   cam=StubCamera("cam")))
    calls: list[int] = []
    ctx.checkpoint = lambda: calls.append(1)        # type: ignore[method-assign]

    lifecycle.initialize(action, ctx)

    assert len(calls) == 3


def test_an_abort_during_initialization_stops_it(fleet, isolated_teach_poses):
    fleet("left", "right")
    abort = threading.Event()
    abort.set()
    action = actions.Initialize(home_after=False)
    ctx = make_ctx(action, Devices(left=make_arm("left"), right=make_arm("right")),
                   abort=abort)
    with pytest.raises(context.ActionAborted):
        lifecycle.initialize(action, ctx)


# --- initialize: the home move (R-INIT-3/4) ----------------------------------------

def test_each_arm_moves_to_its_own_taught_home(fleet, isolated_teach_poses):
    fleet("left", "right")
    isolated_teach_poses.write_text(json.dumps({
        "left": {"HOME": entry(joints=(1, 2, 3, 4, 5, 6))},
        "right": {"HOME": entry(joints=(7, 8, 9, 10, 11, 12))},
    }))
    left, right = make_arm("left"), make_arm("right")
    action = actions.Initialize(home_after=True)

    out = lifecycle.initialize(action, make_ctx(action, Devices(left=left, right=right)))

    assert sorted(out.homed) == ["left", "right"]
    assert out.home_missing == []
    assert left.get_joints() == pytest.approx([1, 2, 3, 4, 5, 6])
    assert right.get_joints() == pytest.approx([7, 8, 9, 10, 11, 12]), (
        "HOME means a different pose on each arm and is never substituted (R-WP-4)")


def test_the_home_move_runs_at_the_slow_tier(fleet, isolated_teach_poses):
    """R-INIT-3. And the *action's* tier must not override it: `lifecycle.initialize` has no
    business homing fast because somebody set the action to fast."""
    fleet("left")
    isolated_teach_poses.write_text(json.dumps({"left": {"HOME": entry()}}))

    class Recording(type(make_arm("left"))):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.speeds: list[float | None] = []

        def move_joints(self, angles, speed=None, wait=True, radius=None):
            self.speeds.append(speed)
            super().move_joints(angles, speed=speed, wait=wait, radius=radius)

    arm = Recording("left", {"type": "mock_arm", "id": "left"})
    action = actions.Initialize(home_after=True, speed="fast")
    lifecycle.initialize(action, make_ctx(action, Devices(left=arm)))

    assert lifecycle.HOME_TIER == "slow"
    assert arm.speeds == [8.0], "ARM_TIERS['slow'] angular, not fast's 45"


def test_an_arm_with_no_taught_home_is_warned_about_and_skipped(fleet,
                                                               isolated_teach_poses):
    """R-INIT-4: a warning, a skip, no guessed home, and initialization does not fail."""
    fleet("left", "right")
    isolated_teach_poses.write_text(json.dumps({"left": {"HOME": entry()}}))
    left, right = make_arm("left"), make_arm("right")
    before = right.get_joints()
    action = actions.Initialize(home_after=True)
    ctx = make_ctx(action, Devices(left=left, right=right))

    out = lifecycle.initialize(action, ctx)

    assert out.home_missing == ["right"]
    assert out.homed == ["left"], "the other arm still homed"
    assert right.get_joints() == pytest.approx(before), "the arm must not have moved"
    warning = next(w for w in ctx.collected_warnings() if w.code == "home_not_defined")
    assert warning.device == "right"
    assert "HOME" in warning.message
    # The whole action succeeded: no home taught is not an initialization failure.
    assert outcome_for(out, "right")["connected"] is True


def test_the_undefined_home_warning_is_matchable_by_code_not_by_message_text(
        fleet, isolated_teach_poses):
    """The readiness panel keys off `code`; matching on prose is how that silently rots."""
    fleet("left")
    action = actions.Initialize(home_after=True)
    ctx = make_ctx(action, Devices(left=make_arm("left")))
    lifecycle.initialize(action, ctx)
    assert [w.code for w in ctx.collected_warnings()] == ["home_not_defined"]


def test_a_home_taught_only_on_the_other_arm_is_never_borrowed(fleet,
                                                               isolated_teach_poses):
    fleet("right")
    isolated_teach_poses.write_text(json.dumps({"left": {"HOME": entry(joints=(1,) * 6)}}))
    right = make_arm("right")
    action = actions.Initialize(home_after=True)

    out = lifecycle.initialize(action, make_ctx(action, Devices(right=right)))

    assert out.home_missing == ["right"]
    assert right.get_joints() == pytest.approx([0.0] * 6)


def test_a_home_that_is_taught_but_unreachable_is_a_different_warning(fleet,
                                                                     isolated_teach_poses):
    """Not `home_missing`: it *is* defined. Still not fatal — the other arm must still home."""
    fleet("left", "right")
    isolated_teach_poses.write_text(json.dumps({
        "left": {"HOME": entry(joints=(0, 0, 0, 0, 0, 300.0))},
        "right": {"HOME": entry(joints=(1, 2, 3, 4, 5, 6))},
    }))
    left = build_driver({"type": "mock_arm", "id": "left",
                         "limits": {"joints": [[-180, 180]] * 6}})
    right = make_arm("right")
    action = actions.Initialize(home_after=True)
    ctx = make_ctx(action, Devices(left=left, right=right))

    out = lifecycle.initialize(action, ctx)

    assert out.home_missing == [], "it is taught, just not reachable"
    assert out.homed == ["right"]
    codes = [w.code for w in ctx.collected_warnings()]
    assert "home_move_failed" in codes and "home_not_defined" not in codes


def test_home_after_false_moves_nothing(fleet, isolated_teach_poses):
    fleet("left")
    isolated_teach_poses.write_text(json.dumps({"left": {"HOME": entry()}}))
    left = make_arm("left")
    action = actions.Initialize(home_after=False)

    out = lifecycle.initialize(action, make_ctx(action, Devices(left=left)))

    assert out.homed == [] and out.home_missing == []
    assert left.get_joints() == pytest.approx([0.0] * 6)


def test_a_device_that_failed_to_connect_is_not_homed(fleet, isolated_teach_poses):
    fleet("left")
    isolated_teach_poses.write_text(json.dumps({"left": {"HOME": entry()}}))

    class Unreachable(type(make_arm("left"))):
        def connect(self):
            raise RuntimeError("no route to host")

    arm = Unreachable("left", {"type": "mock_arm", "id": "left"})
    action = actions.Initialize(home_after=True)
    out = lifecycle.initialize(action, make_ctx(action, Devices(left=arm)))

    assert out.homed == [] and out.home_missing == []
    assert arm.get_joints() == pytest.approx([0.0] * 6)


def test_non_arms_are_not_asked_to_home(fleet, isolated_teach_poses):
    fleet("cam", "ot")
    action = actions.Initialize(home_after=True)
    ctx = make_ctx(action, Devices(cam=StubCamera("cam"),
                                   ot=StubLiquidHandler("ot", with_initialize=True)))

    out = lifecycle.initialize(action, ctx)

    assert out.homed == [] and out.home_missing == []
    assert "home_not_defined" not in [w.code for w in ctx.collected_warnings()]


# --- reconnect (R-ARM-7 / Q5) ------------------------------------------------------

class Engageable(ArmDriver):
    """An arm that records the recovery calls in the order it received them."""
    kind = InstrumentKind.ARM

    def __init__(self, device_id: str = "left", *, error_code: int = 0,
                 refuse_move: bool = False) -> None:
        super().__init__(device_id, {"type": "mock_arm", "id": device_id})
        self.order: list[str] = []
        self.error_code = error_code
        self.refuse_move = refuse_move
        self.moves: list[tuple[float, float, float]] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.device_id, kind=InstrumentKind.ARM)

    def status(self) -> dict:
        return {"error_code": self.error_code, "warn_code": 0}

    def connect(self) -> None:
        self.order.append("connect")
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def clear_errors(self) -> None:
        self.order.append("clear_errors")
        self.error_code = 0

    def enable(self, on: bool = True) -> None:
        self.order.append(f"enable({on})")

    def home(self) -> None: ...

    def stop(self, emergency: bool = False) -> None: ...

    def get_pose(self):
        from drivers.capabilities.arm import Pose
        return Pose(0.0, 0.0, 0.0)

    def move_to(self, pose, speed=None, wait=True) -> None: ...

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0,
                      speed=None, wait=True) -> None:
        self.order.append("move_relative")
        self.moves.append((dx, dy, dz))
        if self.refuse_move:
            raise RuntimeError("mode 0 required")

    def get_joints(self) -> list[float]:
        return [0.0] * 6

    def move_joints(self, angles, speed=None, wait=True, radius=None) -> None: ...

    def move_joints_relative(self, deltas, speed=None, wait=True) -> None: ...

    def grip(self, width=None, force=None) -> None: ...

    def release(self) -> None: ...

    def gripper_width(self):
        return None


def test_engage_clears_errors_then_enables_then_verifies():
    """Q5's definition, in that order. `clear_errors` last would be pointless and `enable`
    first fails on precisely the arm that has a latched fault."""
    arm = Engageable(error_code=19)
    action = actions.Reconnect(device="left", scope="engage")

    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))

    assert arm.order == ["connect", "clear_errors", "enable(True)", "move_relative"]
    assert out.connected and out.enabled and out.verified
    assert out.cleared_errors == ["error_code=19"], "the record says what was wrong"


def test_the_verification_is_a_zero_distance_move():
    """The smallest command that still goes all the way through the controller's accept
    path, so it separates "enabled" from "will actually move" without moving anything."""
    arm = Engageable()
    action = actions.Reconnect(device="left", scope="engage")
    lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))
    assert arm.moves == [(0.0, 0.0, 0.0)]


def test_enabled_and_verified_are_separate_claims():
    """An arm that reports enabled and then refuses the first move has not been recovered,
    and the failure has to surface here rather than at the next real motion."""
    arm = Engageable(refuse_move=True)
    action = actions.Reconnect(device="left", scope="engage")

    with pytest.raises(RuntimeError) as e:
        lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))

    message = str(e.value)
    assert "refused a zero-distance move" in message
    assert "enabled" in message, "the message must say what did succeed"
    assert "mode 0 required" in message, "and what the controller said"


def test_scope_connect_stops_at_connecting():
    arm = Engageable()
    action = actions.Reconnect(device="left", scope="connect")
    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))
    assert arm.order == ["connect"]
    assert out.connected and not out.enabled and not out.verified


def test_scope_enable_connects_and_enables_but_does_not_verify():
    arm = Engageable(error_code=19)
    action = actions.Reconnect(device="left", scope="enable")
    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))
    assert arm.order == ["connect", "enable(True)"]
    assert out.connected and out.enabled and not out.verified
    assert out.cleared_errors == [], "clearing faults belongs to engage"


def test_engage_reports_no_latched_faults_honestly():
    arm = Engageable(error_code=0)
    action = actions.Reconnect(device="left", scope="engage")
    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))
    assert out.cleared_errors == []
    assert out.verified


def test_an_unreadable_status_is_recorded_rather_than_read_as_no_fault():
    """"Cleared nothing" and "could not tell" are different claims."""
    class Opaque(Engageable):
        def status(self):
            raise RuntimeError("controller not responding")

    arm = Opaque()
    action = actions.Reconnect(device="left", scope="engage")
    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=arm)))
    assert out.cleared_errors and "status unreadable" in out.cleared_errors[0]


def test_a_failed_connect_propagates_rather_than_reporting_a_recovery():
    class Unreachable(Engageable):
        def connect(self):
            raise RuntimeError("no route to host")

    action = actions.Reconnect(device="left", scope="engage")
    with pytest.raises(RuntimeError, match="no route to host"):
        lifecycle.reconnect(action, make_ctx(action, Devices(left=Unreachable())))


def test_the_scope_is_echoed_in_the_outputs():
    for scope in ("connect", "enable", "engage"):
        action = actions.Reconnect(device="left", scope=scope)
        out = lifecycle.reconnect(action, make_ctx(action, Devices(left=Engageable())))
        assert out.scope == scope


def test_reconnect_returns_the_outputs_model_its_kind_is_mapped_to():
    action = actions.Reconnect(device="left", scope="engage")
    out = lifecycle.reconnect(action, make_ctx(action, Devices(left=Engageable())))
    assert isinstance(out, actions.OUTPUTS_FOR_KIND["lifecycle.reconnect"])


# --- the home waypoint is the same one the rest of the system uses ------------------

def test_initialization_homes_to_the_waypoint_spec_name(fleet, isolated_teach_poses):
    """There is no `arm.home` kind and no separate home concept: HOME is an ordinary
    per-arm waypoint name (R-WP-4). If this drifts, initialization and the workflow's own
    home moves would go to different places."""
    assert waypoints.HOME == "HOME"
    assert waypoints.owners_of(waypoints.HOME) == ("left", "right")
