"""Arm action handlers (R-ARM-1…6).

The bar here is not "the move happens". It is that every refusal happens **before** the arm
moves, and that the record afterwards says which path ran. Three failures motivate almost
every test below, and all three are silent in a naive implementation:

* one arm sent to a point taught on the other — a collision on a shared table;
* a move refused halfway, leaving the arm somewhere nobody chose;
* a joint replay quietly degraded to an IK solve, so the arm reaches the taught *pose* in a
  different *configuration* and the elbow goes somewhere new.
"""
from __future__ import annotations

import json
import threading

import pytest

import drivers.mock  # noqa: F401  -- registers the mock drivers
from backend.app.engine import actions, blackboard, context
from backend.app.engine.handlers import arm as arm_handlers
from core import waypoints
from core.motion import cap_ops
from drivers import build_driver
from drivers.capabilities.arm import ArmDriver


# --- fixtures ----------------------------------------------------------------------

def entry(*, joints=(0.0, 10.0, 20.0, 30.0, 40.0, 50.0), pose=None) -> dict:
    return {
        "name": "x",
        "pose": pose if pose is not None else {"x": 200.0, "y": 0.0, "z": 300.0,
                                               "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
        "joints": list(joints) if joints else None,
        "gripper_width": 420.0,
        "note": "",
        "saved_at": "2026-07-26T09:00:00+00:00",
    }


def teach(path, library: dict) -> None:
    path.write_text(json.dumps(library))


def make_arm(device_id: str = "right", *, limits=None, joints=None) -> ArmDriver:
    cfg: dict = {"type": "mock_arm", "id": device_id}
    if limits:
        cfg["limits"] = {"joints": limits}
    driver = build_driver(cfg)
    driver.connect()
    if joints:
        driver.move_joints(joints)
    return driver


class Devices:
    """A `context.DeviceAccess` with exactly the drivers a test hands it."""

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
        raise TypeError(f"{device_id} is not a liquid handler")

    def is_simulated(self, device_id: str) -> bool:
        return True


def make_ctx(action, devices: Devices, *, board=None, abort=None, pause=None,
             artifact_dir: str = "") -> context.ActionContext:
    import logging
    ctx = context.ActionContext(
        run_id="test-run", action=action, devices=devices,
        blackboard=board or blackboard.Blackboard(),
        log=logging.getLogger("test.handlers"), simulated=True,
        artifact_dir=artifact_dir,
    )
    ctx._abort = abort
    ctx._pause = pause
    return ctx


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """The mock gripper needs no settle time, and 15 x 0.3 s per decap does not belong in a
    unit test. Patched at module level rather than through the handler, because the handler
    is what builds the `CapConfig`."""
    monkeypatch.setattr(arm_handlers, "DECAP_SETTLE_S", 0.0)


def run(action, devices, **kwargs):
    """Dispatch through the registry, the way the runner will (`actions.handler_for`)."""
    fn = actions.handler_for(action.kind)
    assert fn is not None, f"no handler registered for {action.kind!r}"
    return fn(action, make_ctx(action, devices, **kwargs))


# --- registration ------------------------------------------------------------------

def test_every_arm_kind_has_a_handler():
    """A kind with no handler is a pre-flight failure the engine must report (R-ENG-17), so
    the set being complete is worth asserting rather than discovering at step 7."""
    for kind in ("arm.waypoint", "arm.move_relative", "arm.gripper", "arm.decap",
                 "arm.traverse"):
        assert actions.handler_for(kind) is not None, kind


def test_importing_the_handler_package_is_what_registers_them():
    """R-ENG-17 turns an unregistered kind into "this step cannot run", so *when* the modules
    are imported is load-bearing: the runner's pre-flight has to see the whole registry."""
    from backend.app.engine import handlers

    assert "arm" in handlers.LOADED_MODULES and "lifecycle" in handlers.LOADED_MODULES
    for kind in actions.ACTION_KINDS:
        if kind.startswith(("arm.", "lifecycle.")):
            assert kind in actions.registered_kinds(), kind


def test_the_handler_package_does_not_claim_another_slices_kinds():
    """S3/S4/S5 own camera, liquid-handler and vision handlers. Claiming one here would be a
    duplicate-registration error the moment their module landed."""
    from backend.app.engine import handlers

    assert set(handlers.PENDING_MODULES) == {"camera", "liquid_handler", "vision"}
    for kind in ("camera.snapshot", "camera.search_code", "lh.move_relative",
                 "vision.identify", "vision.solve_offset"):
        assert kind not in ("arm", "lifecycle")
        fn = actions.handler_for(kind)
        assert fn is None or not fn.__module__.endswith(("handlers.arm", "handlers.lifecycle"))


def test_each_handler_returns_the_outputs_model_its_kind_is_mapped_to(isolated_teach_poses):
    teach(isolated_teach_poses, {"right": {"HOME": entry(), "TUBE": entry()}})
    devices = Devices(right=make_arm("right"))
    for action in (
        actions.ArmWaypoint(device="right", waypoint="HOME"),
        actions.ArmRelative(device="right", dz=1.0),
        actions.ArmGripper(device="right", state="open"),
        actions.ArmDecap(device="right"),
        actions.ArmTraverse(device="right", waypoints=["HOME", "TUBE"], blend_deg=None),
    ):
        out = run(action, devices)
        assert isinstance(out, actions.OUTPUTS_FOR_KIND[action.kind]), action.kind


# --- arm.waypoint: ownership and taught-ness (R-WP-1/2/5) ---------------------------

def test_a_waypoint_the_arm_does_not_own_is_refused_naming_owner_and_actor(
        isolated_teach_poses):
    """CAP_GRAB belongs to the left arm. Sending the right arm there is the collision."""
    teach(isolated_teach_poses, {"left": {"CAP_GRAB": entry()}})
    right = make_arm("right")
    before = right.get_joints()

    action = actions.ArmWaypoint(device="right", waypoint="CAP_GRAB")
    with pytest.raises(waypoints.WaypointNotOwned) as e:
        run(action, Devices(right=right, left=make_arm("left")))

    message = str(e.value)
    assert "'left'" in message, "the owner must be named"
    assert "'right'" in message, "the acting device must be named"
    assert right.get_joints() == pytest.approx(before), "nothing may have moved"


def test_the_refusal_happens_before_the_arm_is_asked_anything(isolated_teach_poses):
    """Ownership is a property of the (device, name) pair, so it is decided without reading
    or commanding the arm at all — which is what makes a refusal structurally incapable of
    being a partial move."""
    class Exploding(type(make_arm())):
        def _boom(self, *a, **kw):
            raise AssertionError("the arm was driven before the ownership refusal")

        get_pose = get_joints = _boom
        move_to = move_joints = move_relative = move_joints_relative = _boom
        grip = release = gripper_width = _boom

    arm = Exploding("right", {"type": "mock_arm", "id": "right"})
    with pytest.raises(waypoints.WaypointNotOwned):
        run(actions.ArmWaypoint(device="right", waypoint="CAP_STORE"), Devices(right=arm))


def test_an_untaught_waypoint_is_refused_with_the_device_and_name_pair(isolated_teach_poses):
    teach(isolated_teach_poses, {"right": {}})
    action = actions.ArmWaypoint(device="right", waypoint="TUBE")
    with pytest.raises(waypoints.WaypointNotTaught) as e:
        run(action, Devices(right=make_arm("right")))
    assert e.value.pair == ("right", "TUBE"), "R-WP-5: pairs, never bare names"


def test_home_is_resolved_per_arm_and_never_substituted(isolated_teach_poses):
    """Both arms own the name HOME and it means a different pose on each (R-WP-4)."""
    teach(isolated_teach_poses, {"left": {"HOME": entry(joints=(1, 2, 3, 4, 5, 6))}})
    with pytest.raises(waypoints.WaypointNotTaught):
        run(actions.ArmWaypoint(device="right", waypoint="HOME"),
            Devices(right=make_arm("right"), left=make_arm("left")))


# --- arm.waypoint: joint replay, offsets, pre-flight ---------------------------------

def test_joint_replay_is_preferred_when_the_joints_were_recorded(isolated_teach_poses):
    """Those angles were physically reached, so there is no IK branch to guess at."""
    taught = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0)
    teach(isolated_teach_poses, {"right": {"TUBE": entry(joints=taught)}})
    arm = make_arm("right")
    pose_before = arm.get_pose()

    out = run(actions.ArmWaypoint(device="right", waypoint="TUBE"), Devices(right=arm))

    assert arm.get_joints() == pytest.approx(list(taught))
    assert out.resolved_speed["path"] == arm_handlers.JOINT_REPLAY
    assert out.joints_after == pytest.approx(list(taught))
    # The mock's pose is only changed by cartesian commands, so an untouched pose proves no
    # cartesian move was issued.
    assert arm.get_pose().__dict__ == pose_before.__dict__


def test_a_waypoint_stored_without_joints_falls_back_to_cartesian(isolated_teach_poses):
    pose = {"x": 250.0, "y": 12.0, "z": 310.0, "roll": 180.0, "pitch": 0.0, "yaw": 0.0}
    teach(isolated_teach_poses, {"right": {"TUBE": entry(joints=None, pose=pose)}})
    arm = make_arm("right")
    joints_before = arm.get_joints()

    out = run(actions.ArmWaypoint(device="right", waypoint="TUBE"), Devices(right=arm))

    assert out.resolved_speed["path"] == arm_handlers.CARTESIAN
    assert arm.get_pose().x == pytest.approx(250.0)
    assert arm.get_joints() == pytest.approx(joints_before), "no joint command was issued"


def test_joints_taught_on_a_different_axis_count_fall_back_rather_than_replay(
        isolated_teach_poses):
    """Replaying five angles on a six-axis arm would leave J6 wherever it happened to be."""
    teach(isolated_teach_poses, {"right": {"TUBE": entry(joints=(1, 2, 3, 4, 5))}})
    arm = make_arm("right")
    out = run(actions.ArmWaypoint(device="right", waypoint="TUBE"), Devices(right=arm))
    assert out.resolved_speed["path"] == arm_handlers.CARTESIAN
    assert arm.get_joints() == pytest.approx([0.0] * 6)


def test_an_offset_replays_the_joints_then_applies_it_cartesian(isolated_teach_poses):
    """R-ARM-2. Joint replay reaches the taught configuration exactly; the offset is the
    only part handed to IK, and the outputs say so."""
    taught = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0)
    pose = {"x": 200.0, "y": 0.0, "z": 300.0, "roll": 180.0, "pitch": 0.0, "yaw": 0.0}
    teach(isolated_teach_poses, {"right": {"TUBE": entry(joints=taught, pose=pose)}})
    arm = make_arm("right")

    out = run(actions.ArmWaypoint(device="right", waypoint="TUBE", dz=-5.0, dx=2.5),
              Devices(right=arm))

    assert out.resolved_speed["path"] == arm_handlers.JOINT_REPLAY_THEN_OFFSET
    assert arm.get_joints() == pytest.approx(list(taught)), "the taught joints were replayed"
    assert out.offsets_mm == {"dx": 2.5, "dy": 0.0, "dz": -5.0}
    # The mock applies a relative move to its own pose, which starts at the default, so
    # what is asserted here is that a relative delta was issued — the offset, not the pose.
    assert arm.get_pose().z == pytest.approx(200.0 - 5.0)
    assert arm.get_pose().x == pytest.approx(200.0 + 2.5)


def test_a_zero_offset_is_still_a_plain_joint_replay(isolated_teach_poses):
    """Defaults are 0, so the common case must not be pushed onto the offset path."""
    teach(isolated_teach_poses, {"right": {"TUBE": entry()}})
    out = run(actions.ArmWaypoint(device="right", waypoint="TUBE", dx=0.0, dy=0.0, dz=0.0),
              Devices(right=make_arm("right")))
    assert out.resolved_speed["path"] == arm_handlers.JOINT_REPLAY


def test_a_target_outside_a_joint_soft_limit_is_refused_instead_of_moved(
        isolated_teach_poses):
    teach(isolated_teach_poses, {"right": {"TUBE": entry(joints=(0, 0, 0, 0, 0, 300.0))}})
    arm = make_arm("right", limits=[[-180, 180]] * 6)
    before = arm.get_joints()

    with pytest.raises(ValueError) as e:
        run(actions.ArmWaypoint(device="right", waypoint="TUBE"), Devices(right=arm))

    assert "J6" in str(e.value)
    assert "refused before moving" in str(e.value)
    assert arm.get_joints() == pytest.approx(before), "a refusal must not be a partial move"


def test_the_offset_half_is_pre_flighted_before_the_joint_half_runs(isolated_teach_poses):
    """Otherwise a legal replay lands somewhere the offset cannot legally leave, and the
    arm has to be recovered by hand."""
    class NoLowZ(type(make_arm())):
        def check_pose_target(self, pose):
            return f"z={pose.z:.1f} is below the deck" if pose.z < 250.0 else None

    teach(isolated_teach_poses, {"right": {"TUBE": entry()}})
    arm = NoLowZ("right", {"type": "mock_arm", "id": "right"})
    arm.connect()
    before = arm.get_joints()

    with pytest.raises(ValueError, match="below the deck"):
        run(actions.ArmWaypoint(device="right", waypoint="TUBE", dz=-100.0),
            Devices(right=arm))
    assert arm.get_joints() == pytest.approx(before), "the joint half must not have run"


def test_the_resolved_speed_is_the_tier_clamped_by_this_arms_limits(isolated_teach_poses):
    teach(isolated_teach_poses, {"right": {"HOME": entry()}})
    out = run(actions.ArmWaypoint(device="right", waypoint="HOME", speed="slow"),
              Devices(right=make_arm("right")))
    assert out.resolved_speed["tier"] == "slow"
    assert out.resolved_speed["angular"] == 8.0        # ARM_TIERS["slow"]


def test_a_waypoint_move_does_not_touch_the_gripper(isolated_teach_poses):
    """The taught entry carries a gripper width; replaying it would make opening the jaws a
    side effect of a move rather than a plan step an operator can see."""
    teach(isolated_teach_poses, {"right": {"HOME": entry()}})
    arm = make_arm("right")
    width = arm.gripper_width()
    run(actions.ArmWaypoint(device="right", waypoint="HOME"), Devices(right=arm))
    assert arm.gripper_width() == pytest.approx(width)


# --- arm.move_relative --------------------------------------------------------------

def test_a_relative_move_applies_the_literal_deltas():
    arm = make_arm("right")
    out = run(actions.ArmRelative(device="right", dx=1.0, dy=-2.0, dz=3.0, dyaw=4.0),
              Devices(right=arm))
    pose = arm.get_pose()
    assert (pose.x, pose.y, pose.z) == pytest.approx((201.0, -2.0, 203.0))
    assert pose.yaw == pytest.approx(4.0)
    assert out.offsets_mm == {"dx": 1.0, "dy": -2.0, "dz": 3.0}


def test_a_relative_move_can_read_its_deltas_from_a_blackboard_slot():
    board = blackboard.Blackboard()
    board.set("selected_offset", actions.OffsetOutputs(
        residual_offset_mm={"x": 1.5, "y": -0.5, "z": 2.0}, method="tag_3d"))
    arm = make_arm("right")

    action = actions.ArmRelative(device="right", from_slot="selected_offset", dx=99.0)
    out = run(action, Devices(right=arm), board=board)

    assert out.offsets_mm == {"dx": 1.5, "dy": -0.5, "dz": 2.0}
    assert arm.get_pose().x == pytest.approx(201.5), "the slot wins over the literal field"


@pytest.mark.parametrize("value", [
    {"x": 1.5, "y": -0.5, "z": 2.0},                             # a bare point
    {"dx": 1.5, "dy": -0.5, "dz": 2.0},                          # a bare delta
    {"residual_offset_mm": {"x": 1.5, "y": -0.5, "z": 2.0}},     # a dumped OffsetOutputs
])
def test_the_three_slot_shapes_a_producer_can_write_are_all_read(value):
    """One dict lookup and a field read in Python — no dotted-path resolver (review S2). The
    shapes are enumerated here so a new producer writing a fourth one fails a test rather
    than silently commanding a zero move."""
    board = blackboard.Blackboard()
    board.set("offset", value)
    arm = make_arm("right")
    out = run(actions.ArmRelative(device="right", from_slot="offset"),
              Devices(right=arm), board=board)
    assert out.offsets_mm == {"dx": 1.5, "dy": -0.5, "dz": 2.0}


def test_an_unobservable_axis_in_a_slot_warns_and_commands_no_motion_on_it():
    """`None` and `0.0` mean opposite things to a servo loop, so the difference has to be
    reachable rather than absorbed (R-LOG-6)."""
    board = blackboard.Blackboard()
    board.set("offset", {"residual_offset_mm": {"x": 2.0, "y": None, "z": None}})
    arm = make_arm("right")
    action = actions.ArmRelative(device="right", from_slot="offset")
    ctx = make_ctx(action, Devices(right=arm), board=board)

    out = arm_handlers.move_relative(action, ctx)

    assert out.offsets_mm == {"dx": 2.0, "dy": 0.0, "dz": 0.0}
    codes = [w.code for w in ctx.collected_warnings()]
    assert codes.count("unobservable_axis") == 2


def test_a_slot_with_nothing_observable_is_refused_not_run_as_a_zero_move():
    board = blackboard.Blackboard()
    board.set("offset", {"residual_offset_mm": {"x": None, "y": None, "z": None}})
    arm = make_arm("right")
    before = arm.get_pose().__dict__
    with pytest.raises(ValueError, match="no observable axis"):
        run(actions.ArmRelative(device="right", from_slot="offset"),
            Devices(right=arm), board=board)
    assert arm.get_pose().__dict__ == before


def test_an_empty_slot_raises_rather_than_moving_by_zero():
    """`SlotEmpty` is deliberately not `None`: "nothing written yet" is not "no offset"."""
    with pytest.raises(blackboard.SlotEmpty):
        run(actions.ArmRelative(device="right", from_slot="offset"),
            Devices(right=make_arm("right")))


def test_a_relative_move_whose_result_leaves_the_envelope_is_refused():
    class NoLowZ(type(make_arm())):
        def check_pose_target(self, pose):
            return "below the deck" if pose.z < 100.0 else None

    arm = NoLowZ("right", {"type": "mock_arm", "id": "right"})
    arm.connect()
    before = arm.get_pose().__dict__
    with pytest.raises(ValueError, match="below the deck"):
        run(actions.ArmRelative(device="right", dz=-150.0), Devices(right=arm))
    assert arm.get_pose().__dict__ == before


# --- arm.gripper --------------------------------------------------------------------

def test_open_and_close_drive_the_jaws():
    arm = make_arm("right")
    closed = run(actions.ArmGripper(device="right", state="close"), Devices(right=arm))
    assert arm.gripper_width() == pytest.approx(0.0)
    assert closed.width_after == pytest.approx(0.0)

    opened = run(actions.ArmGripper(device="right", state="open"), Devices(right=arm))
    assert arm.gripper_width() == pytest.approx(850.0)
    assert opened.width_before == pytest.approx(0.0)
    assert opened.width_after == pytest.approx(850.0)


def test_a_width_is_commanded_exactly():
    arm = make_arm("right")
    out = run(actions.ArmGripper(device="right", state="close", width=420.0),
              Devices(right=arm))
    assert arm.gripper_width() == pytest.approx(420.0)
    assert out.width_after == pytest.approx(420.0)


def test_an_out_of_range_width_is_refused_and_not_clamped():
    """Clamping silently turns a unit-conversion bug — metres handed to a counts API — into
    a crushed payload, so the answer is a refusal that names the real range."""
    arm = make_arm("right")
    before = arm.gripper_width()
    for bad in (-1.0, 851.0, 9000.0):
        with pytest.raises(ValueError) as e:
            run(actions.ArmGripper(device="right", state="close", width=bad),
                Devices(right=arm))
        assert "refusing rather than clamping" in str(e.value)
        assert "0..850" in str(e.value)
        assert arm.gripper_width() == pytest.approx(before), "the jaws must not have moved"


def test_opening_to_a_width_is_the_same_command_as_closing_to_it():
    """A parallel gripper has one "go to this opening" call; whether it reads as opening or
    closing depends only on where the jaws are. `state` stays meaningful for the plan and the
    log, and the width is what is commanded."""
    arm = make_arm("right")
    arm.grip(width=100.0)
    out = run(actions.ArmGripper(device="right", state="open", width=600.0),
              Devices(right=arm))
    assert arm.gripper_width() == pytest.approx(600.0)
    assert out.state == "open"
    assert out.width_before == pytest.approx(100.0)


def test_a_width_on_a_gripper_that_has_none_is_refused():
    class NoWidth(type(make_arm())):
        @property
        def gripper_info(self):
            from drivers.capabilities.arm import GripperInfo
            return GripperInfo(kind="lite6", supports_width=False)

    arm = NoWidth("right", {"type": "mock_arm", "id": "right"})
    arm.connect()
    with pytest.raises(ValueError, match="no commandable width"):
        run(actions.ArmGripper(device="right", state="close", width=100.0),
            Devices(right=arm))


# --- arm.decap (R-ARM-5 / D12) ------------------------------------------------------

def test_decap_takes_four_ninety_degree_bites_and_turns_the_cap_once():
    arm = make_arm("left")
    arm.grip(width=420.0)
    joints_before = arm.get_joints()

    out = run(actions.ArmDecap(device="left", grip_counts=420.0), Devices(left=arm))

    assert out.bites == 4
    assert out.step_deg == 90.0
    assert out.total_rotation_deg == pytest.approx(360.0)
    assert out.net_wrist_travel_deg == pytest.approx(0.0)
    assert out.preflight_ok is True
    assert arm.get_joints() == pytest.approx(joints_before), "net-zero wrist travel"
    assert arm.gripper_width() == pytest.approx(850.0), "the cap must be left released"


def test_the_decap_plan_is_assertable_without_an_arm():
    """The plan is a pure function, so the sequence — the part that would screw the cap back
    on if the open/close were the wrong way round — is checkable on its own."""
    steps = cap_ops.plan_ratchet(90.0, 360.0)
    assert cap_ops.count_bites(steps) == 4
    assert cap_ops.gripped_rotation(steps) == pytest.approx(360.0)
    assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0)
    assert steps[-2:] == [("open", 0.0), ("turn", -90.0)], "ends released and unwound"


def test_decap_rotates_only_the_tool_axis_in_joint_space():
    """Never a cartesian yaw: asking IK for a turn invites a different arm configuration."""
    arm = make_arm("left")
    pose_before = arm.get_pose().__dict__
    run(actions.ArmDecap(device="left"), Devices(left=arm))
    assert arm.get_pose().__dict__ == pose_before, "no cartesian command may be issued"


def test_a_decap_that_would_exceed_a_joint_soft_limit_is_refused_before_the_first_step():
    """Halfway is the worst outcome: the cap is part unscrewed and the wrist is wound."""
    limits = [[-360, 360]] * 5 + [[-360, 150]]
    arm = make_arm("left", limits=limits, joints=[0, 0, 0, 0, 0, 100.0])
    arm.grip(width=420.0)

    with pytest.raises(cap_ops.CapOpError) as e:
        run(actions.ArmDecap(device="left", grip_counts=420.0), Devices(left=arm))

    assert "J6" in str(e.value)
    assert "before starting" in str(e.value)
    assert arm.get_joints()[-1] == pytest.approx(100.0), "not one bite may have run"
    assert arm.gripper_width() == pytest.approx(420.0), "the jaws must not have opened"


def test_every_intermediate_angle_is_pre_flighted_not_just_the_endpoints():
    """The plan returns the wrist to its start, so a check on the endpoints alone passes a
    plan whose middle drives J6 straight past its limit."""
    limits = [[-360, 360]] * 5 + [[-45, 45]]
    arm = make_arm("left", limits=limits)          # starts at 0 — endpoints are fine
    with pytest.raises(cap_ops.CapOpError):
        run(actions.ArmDecap(device="left", step_deg=90.0), Devices(left=arm))
    assert arm.get_joints()[-1] == pytest.approx(0.0)


def test_a_smaller_bite_makes_the_same_decap_legal():
    """The concrete reason D12 chose 90 over 180: peak excursion is one bite."""
    limits = [[-360, 360]] * 5 + [[-60, 60]]
    arm = make_arm("left", limits=limits)
    out = run(actions.ArmDecap(device="left", step_deg=45.0), Devices(left=arm))
    assert out.bites == 8
    assert out.total_rotation_deg == pytest.approx(360.0)


def test_checkpoint_is_called_between_decap_bites():
    arm = make_arm("left")
    action = actions.ArmDecap(device="left")
    ctx = make_ctx(action, Devices(left=arm))
    calls: list[int] = []
    ctx.checkpoint = lambda: calls.append(1)      # type: ignore[method-assign]

    arm_handlers.decap(action, ctx)

    assert len(calls) == 4, "one per bite, so a pause lands between them"


def test_an_abort_mid_decap_stops_between_bites_with_the_wrist_home():
    """A bite is atomic. Aborting at a checkpoint therefore leaves the jaws open and the
    wrist exactly where it started — the only state worth resuming or recovering from."""
    arm = make_arm("left")
    arm.grip(width=420.0)
    joints_before = arm.get_joints()
    abort = threading.Event()
    action = actions.ArmDecap(device="left", grip_counts=420.0)
    ctx = make_ctx(action, Devices(left=arm), abort=abort)

    real_checkpoint = ctx.checkpoint
    seen = {"bites": 0}

    def checkpoint():
        seen["bites"] += 1
        if seen["bites"] == 2:
            abort.set()
        real_checkpoint()

    ctx.checkpoint = checkpoint                   # type: ignore[method-assign]
    with pytest.raises(context.ActionAborted):
        arm_handlers.decap(action, ctx)

    assert seen["bites"] == 2, "it stopped at the second bite boundary, not later"
    assert arm.get_joints() == pytest.approx(joints_before), "wrist back where it started"
    assert arm.gripper_width() == pytest.approx(850.0), "jaws open, cap not held"


def test_a_pause_mid_decap_blocks_and_then_resumes_to_completion():
    arm = make_arm("left")
    pause = threading.Event()                     # cleared = paused
    action = actions.ArmDecap(device="left")
    ctx = make_ctx(action, Devices(left=arm), pause=pause)
    out: dict = {}

    worker = threading.Thread(target=lambda: out.update(
        result=arm_handlers.decap(action, ctx)))
    worker.start()
    worker.join(timeout=1.0)
    assert worker.is_alive(), "the handler must block at its first checkpoint while paused"

    pause.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert out["result"].bites == 4


def test_the_emergency_stop_stays_reachable_while_a_handler_is_paused():
    """R-ENG-11/D21. `POST /api/arms/{id}/stop` bypasses the teach lock on purpose; an
    engine that took a per-arm claim in its handlers would make the stop path unreachable
    exactly when it is needed. There is no such claim, and this is the tripwire."""
    arm = make_arm("left")
    pause = threading.Event()
    action = actions.ArmDecap(device="left")
    ctx = make_ctx(action, Devices(left=arm), pause=pause)

    worker = threading.Thread(target=lambda: arm_handlers.decap(action, ctx), daemon=True)
    worker.start()
    worker.join(timeout=0.5)
    assert worker.is_alive(), "the handler is mid-action and paused"

    stopped = threading.Event()
    threading.Thread(target=lambda: (arm.stop(emergency=True), stopped.set()),
                     daemon=True).start()
    assert stopped.wait(timeout=2.0), "the stop path blocked behind the running action"

    pause.set()
    worker.join(timeout=5.0)


def test_a_wrist_that_does_not_return_is_warned_about_rather_than_hidden():
    class Drifting(type(make_arm())):
        def move_joints_relative(self, deltas, speed=None, wait=True):
            super().move_joints_relative(deltas, speed=speed, wait=wait)
            self._joints[-1] += 1.0        # a slipping tool axis

    arm = Drifting("left", {"type": "mock_arm", "id": "left"})
    arm.connect()
    action = actions.ArmDecap(device="left")
    ctx = make_ctx(action, Devices(left=arm))

    out = arm_handlers.decap(action, ctx)

    assert out.net_wrist_travel_deg == pytest.approx(8.0)
    assert [w.code for w in ctx.collected_warnings()] == ["wrist_did_not_return"]


def test_decap_uses_the_slow_tier_by_default():
    """`ArmDecap.speed` defaults to slow, and unscrewing is not a place to hurry."""
    assert actions.ArmDecap(device="left").speed == "slow"


def test_an_out_of_range_grip_width_is_refused_before_the_ratchet_starts():
    arm = make_arm("left")
    joints_before = arm.get_joints()
    with pytest.raises(ValueError, match="refusing rather than clamping"):
        run(actions.ArmDecap(device="left", grip_counts=9000.0), Devices(left=arm))
    assert arm.get_joints() == pytest.approx(joints_before)


# --- arm.traverse (R-ARM-6) ---------------------------------------------------------

TRAVERSE = ["APPROACH_TUBE_TRANSFER", "TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE",
            "TRANSITION_LIQUID_HANDLER_TABLE"]


def traverse_library(**overrides) -> dict:
    library = {name: entry(joints=(0.0, 0.0, 0.0, 0.0, 0.0, 10.0 * i))
               for i, name in enumerate(TRAVERSE)}
    library.update(overrides)
    return {"right": library}


def test_a_traverse_visits_every_waypoint_in_order(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())
    arm = make_arm("right")
    out = run(actions.ArmTraverse(device="right", waypoints=TRAVERSE), Devices(right=arm))
    assert out.reached == TRAVERSE
    assert arm.get_joints()[-1] == pytest.approx(30.0), "it ended on the last waypoint"


def test_every_waypoint_of_a_traverse_is_ownership_checked(isolated_teach_poses):
    """One wrong name in the middle of a list is the same collision as one on its own."""
    library = traverse_library()
    library["left"] = {"CAP_STORE": entry()}
    teach(isolated_teach_poses, library)
    arm = make_arm("right")
    before = arm.get_joints()

    with pytest.raises(waypoints.WaypointNotOwned) as e:
        run(actions.ArmTraverse(device="right",
                                waypoints=[TRAVERSE[0], "CAP_STORE", TRAVERSE[1]]),
            Devices(right=arm))
    assert "'left'" in str(e.value) and "'right'" in str(e.value)
    assert arm.get_joints() == pytest.approx(before), "not one leg may have run"


def test_an_untaught_waypoint_late_in_a_traverse_refuses_before_the_first_move(
        isolated_teach_poses):
    library = traverse_library()
    del library["right"][TRAVERSE[3]]
    teach(isolated_teach_poses, library)
    arm = make_arm("right")
    before = arm.get_joints()

    with pytest.raises(waypoints.WaypointNotTaught) as e:
        run(actions.ArmTraverse(device="right", waypoints=TRAVERSE), Devices(right=arm))
    assert e.value.pair == ("right", TRAVERSE[3])
    assert arm.get_joints() == pytest.approx(before)


def test_a_soft_limit_violation_late_in_a_traverse_refuses_before_the_first_move(
        isolated_teach_poses):
    library = traverse_library(**{TRAVERSE[3]: entry(joints=(0, 0, 0, 0, 0, 300.0))})
    teach(isolated_teach_poses, library)
    arm = make_arm("right", limits=[[-180, 180]] * 6)
    before = arm.get_joints()

    with pytest.raises(ValueError, match="refused before moving"):
        run(actions.ArmTraverse(device="right", waypoints=TRAVERSE), Devices(right=arm))
    assert arm.get_joints() == pytest.approx(before), (
        "stopping halfway leaves the arm over the deck — that is the failure R-ARM-6 names")


def test_progress_is_reported_per_waypoint(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())
    action = actions.ArmTraverse(device="right", waypoints=TRAVERSE)
    ctx = make_ctx(action, Devices(right=make_arm("right")))
    seen: list[str] = []
    ctx._on_progress = lambda message, fields: seen.append(message)

    arm_handlers.traverse(action, ctx)

    assert len(seen) == 4
    for name, message in zip(TRAVERSE, seen):
        assert name in message


def test_checkpoint_is_called_between_traverse_waypoints(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())
    action = actions.ArmTraverse(device="right", waypoints=TRAVERSE)
    ctx = make_ctx(action, Devices(right=make_arm("right")))
    calls: list[int] = []
    ctx.checkpoint = lambda: calls.append(1)       # type: ignore[method-assign]

    arm_handlers.traverse(action, ctx)

    assert len(calls) == 3, "between waypoints, not before the first"


def test_an_abort_mid_traverse_stops_on_a_taught_waypoint(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())
    arm = make_arm("right")
    abort = threading.Event()
    action = actions.ArmTraverse(device="right", waypoints=TRAVERSE)
    ctx = make_ctx(action, Devices(right=arm), abort=abort)

    real_checkpoint = ctx.checkpoint

    def checkpoint():
        abort.set()
        real_checkpoint()

    ctx.checkpoint = checkpoint                    # type: ignore[method-assign]
    with pytest.raises(context.ActionAborted):
        arm_handlers.traverse(action, ctx)

    assert arm.get_joints()[-1] == pytest.approx(0.0), (
        "it stopped on the first waypoint, which is a taught pose")


def test_the_blend_radius_is_clamped_to_the_shortest_segment_and_the_clamp_is_reported(
        isolated_teach_poses):
    """The controller rejects a radius longer than the track, and a blend eats radius at
    both ends of a segment — so the report has to be the value used, not the one asked for."""
    teach(isolated_teach_poses, traverse_library())     # 10 deg per segment
    out = run(actions.ArmTraverse(device="right", waypoints=TRAVERSE, blend_deg=45.0),
              Devices(right=make_arm("right")))
    assert out.blend_deg == pytest.approx(5.0), "half of the 10 deg shortest segment"


def test_a_blend_that_fits_is_left_alone(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())
    out = run(actions.ArmTraverse(device="right", waypoints=TRAVERSE, blend_deg=2.0),
              Devices(right=make_arm("right")))
    assert out.blend_deg == pytest.approx(2.0)


def test_blending_queues_the_intermediate_moves_and_waits_once_at_the_end(
        isolated_teach_poses):
    """Blending only means anything if the moves do not block, which in turn is why the
    action is not finished until `wait_for_idle` says so."""
    teach(isolated_teach_poses, traverse_library())

    class Recording(type(make_arm())):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls: list[tuple[bool, float | None]] = []
            self.waited = 0

        def move_joints(self, angles, speed=None, wait=True, radius=None):
            self.calls.append((wait, radius))
            super().move_joints(angles, speed=speed, wait=wait, radius=radius)

        def wait_for_idle(self, timeout: float = 60.0) -> bool:
            self.waited += 1
            return True

    arm = Recording("right", {"type": "mock_arm", "id": "right"})
    arm.connect()
    run(actions.ArmTraverse(device="right", waypoints=TRAVERSE, blend_deg=2.0),
        Devices(right=arm))

    assert arm.calls[:-1] == [(False, 2.0)] * 3, "intermediate legs queue with a radius"
    assert arm.calls[-1] == (True, None), "the last leg blocks and does not blend"
    assert arm.waited == 1


def test_point_to_point_is_what_no_blend_radius_means(isolated_teach_poses):
    teach(isolated_teach_poses, traverse_library())

    class Recording(type(make_arm())):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls: list[tuple[bool, float | None]] = []

        def move_joints(self, angles, speed=None, wait=True, radius=None):
            self.calls.append((wait, radius))
            super().move_joints(angles, speed=speed, wait=wait, radius=radius)

    arm = Recording("right", {"type": "mock_arm", "id": "right"})
    arm.connect()
    out = run(actions.ArmTraverse(device="right", waypoints=TRAVERSE, blend_deg=None),
              Devices(right=arm))

    assert out.blend_deg is None
    assert arm.calls == [(True, None)] * 4, "every leg blocks; nothing is queued"


def test_a_cartesian_only_waypoint_makes_the_path_unblendable_and_says_so(
        isolated_teach_poses):
    library = traverse_library(**{TRAVERSE[2]: entry(joints=None)})
    teach(isolated_teach_poses, library)
    action = actions.ArmTraverse(device="right", waypoints=TRAVERSE, blend_deg=5.0)
    ctx = make_ctx(action, Devices(right=make_arm("right")))

    out = arm_handlers.traverse(action, ctx)

    assert out.blend_deg is None
    assert [w.code for w in ctx.collected_warnings()] == ["blend_unavailable"]
    assert out.reached == TRAVERSE
