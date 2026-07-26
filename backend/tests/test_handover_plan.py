"""The hero workflow's plan data, checked against the waypoint spec that owns it.

The load-bearing test here is `test_every_waypoint_is_owned_by_the_arm_that_visits_it`. The
whole per-arm ownership mechanism exists to stop one arm being sent to the other's taught
point, and a plan is exactly where that mistake gets written down — the brief itself named
five right-arm waypoints with a `LEFT_ARM_` prefix. Checking the plan against the spec
statically means the mistake cannot reach a robot.
"""
from __future__ import annotations

import pytest

from backend.app.engine.actions import (ArmDecap, ArmGripper, ArmTraverse,
                                        ArmWaypoint, CameraSnapshot, Loop,
                                        VisionIdentify, VisionSolveOffset)
from backend.app.engine.plans import handover
from core import waypoints


def _flat(actions):
    """Every action, loop bodies included."""
    for a in actions:
        yield a
        if isinstance(a, Loop):
            yield from _flat(list(a.body))


# --- the ownership cross-check ------------------------------------------------

def test_every_waypoint_is_owned_by_the_arm_that_visits_it():
    """No action may send an arm to a waypoint the spec gives to the other one.

    This is the error the ownership rule exists to prevent, and the plan is where it would
    be written. Note the five waypoints the brief prefixed `LEFT_ARM_` are visited by the
    RIGHT arm here — deliberately, because the right arm is the one carrying the tube.
    """
    for device, name in handover.waypoints_used():
        spec = waypoints.spec_for(device, name)
        assert spec is not None, (
            f"the plan sends {device!r} to {name!r}, which is not in the waypoint spec for "
            f"that arm. Owners of {name!r}: {waypoints.owners_of(name) or 'nobody'}"
        )
        assert spec.device == device


def test_the_transition_and_deck_waypoints_belong_to_the_right_arm():
    """The brief's `LEFT_ARM_` prefix on these was a naming slip, not a device change."""
    for name in ("TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE",
                 "TRANSITION_LIQUID_HANDLER_TABLE", "LIQUID_HANDLER_APPROACH_DECK",
                 "LIQUID_HANDLER_DECK"):
        assert waypoints.owners_of(name) == ("right",)
    visited = handover.waypoints_used()
    assert ("right", "TRANSITION_MID_TABLE") in visited
    assert ("left", "TRANSITION_MID_TABLE") not in visited


def test_sending_the_left_arm_to_a_right_arm_waypoint_is_refused():
    """The guard behind the plan check: the resolver itself refuses the pairing."""
    with pytest.raises(waypoints.WaypointNotOwned) as exc:
        waypoints.assert_owned("left", "LIQUID_HANDLER_DECK")
    assert "right" in str(exc.value)


# --- plan shape ---------------------------------------------------------------

def test_the_plan_round_trips_through_the_action_union():
    """A typo in a field name must fail here, not as a handler seeing a silent default."""
    assert len(handover.validate()) == len(handover.build())


def test_both_grippers_open_before_anything_moves():
    """Neither arm may arrive at a tube or a cap already holding something."""
    plan = handover.build()
    opens = [a for a in plan[:2] if isinstance(a, ArmGripper) and a.state == "open"]
    assert {a.device for a in opens} == {"left", "right"}
    assert not any(isinstance(a, (ArmWaypoint, ArmTraverse)) for a in plan[:2])


def _step_indices(plan):
    """`(kind, waypoint-or-state)` per action, for ordering assertions."""
    return [(type(a).__name__, getattr(a, "waypoint", getattr(a, "state", ""))) for a in plan]


def test_the_tube_is_gripped_before_it_is_lifted():
    """Close on the tube at TUBE, and only then go to the transfer pose."""
    plan = handover.build()
    kinds = _step_indices(plan)
    at_tube = kinds.index(("ArmWaypoint", "TUBE"))
    close = next(i for i, (n, v) in enumerate(kinds)
                 if n == "ArmGripper" and v == "close" and plan[i].device == "right")
    lift = kinds.index(("ArmWaypoint", "APPROACH_TUBE_TRANSFER"))
    assert at_tube < close < lift


def test_the_tube_stays_seated_in_the_rack_until_the_cap_is_off_and_stored():
    """The tube must not leave the rack until uncapping is finished and the cap is stored.

    Operator-specified, and a safety constraint rather than a preference — two independent
    reasons, either of which is sufficient:

    **Torque reaction.** Unscrewing applies torque to the cap and the tube has to resist it.
    Seated in the rack, the rack takes that reaction load. Lifted to the transfer pose first,
    the only thing resisting four 90° bites is the right arm's grip on a smooth tube at 35 %
    — so the tube twists in the jaws, the cap does not come off, and the arm is side-loaded
    through the whole ratchet.

    **Arm-to-arm clearance.** `APPROACH_CAP_STORE -> CAP_STORE` is a 416 mm sweep across the
    bench. Raising the tube into that path risks a collision neither controller can predict:
    each models only its own links, which is the same blind spot that made the flange-camera
    J5 limit necessary.

    This test would have failed on the original plan, where the lift was step 7.
    """
    plan = handover.build()
    kinds = _step_indices(plan)

    lift = kinds.index(("ArmWaypoint", "APPROACH_TUBE_TRANSFER"))
    decap = next(i for i, a in enumerate(plan) if isinstance(a, ArmDecap))
    cap_store = kinds.index(("ArmWaypoint", "CAP_STORE"))
    cap_released = next(i for i, a in enumerate(plan)
                        if i > decap and isinstance(a, ArmGripper)
                        and a.device == "left" and a.state == "open")

    assert decap < lift, "the tube was lifted before the cap was unscrewed"
    assert cap_store < lift, "the tube was lifted before the cap reached its store"
    assert cap_released < lift, "the tube was lifted before the left arm let the cap go"


def test_nothing_the_right_arm_does_happens_between_the_cap_grab_and_the_cap_release():
    """While the left arm works on the cap, the right arm holds still.

    Any right-arm motion in that window is either the tube moving under an applied torque or
    two arms moving in a shared volume. The right arm's next action after the grip must be the
    lift, and that must come after the cap is released.
    """
    plan = handover.build()
    kinds = _step_indices(plan)
    first_cap = kinds.index(("ArmWaypoint", "APPROACH_CAP_GRAB"))
    cap_released = next(i for i, a in enumerate(plan)
                        if i > first_cap and isinstance(a, ArmGripper)
                        and a.device == "left" and a.state == "open")

    moved = [i for i in range(first_cap, cap_released)
             if plan[i].device == "right"
             and isinstance(plan[i], (ArmWaypoint, ArmTraverse, ArmDecap))]
    assert not moved, (
        f"the right arm moves at index {moved} while the left arm is working on the cap"
    )


def test_the_cap_is_gripped_before_decap_and_released_after_the_store_move():
    plan = handover.build()
    decap = next(i for i, a in enumerate(plan) if isinstance(a, ArmDecap))
    close = next(i for i, a in enumerate(plan)
                 if isinstance(a, ArmGripper) and a.device == "left" and a.state == "close")
    store = next(i for i, a in enumerate(plan)
                 if isinstance(a, ArmWaypoint) and a.waypoint == "CAP_STORE")
    release = next(i for i, a in enumerate(plan)
                   if i > decap and isinstance(a, ArmGripper) and a.device == "left"
                   and a.state == "open")
    assert close < decap < store < release


def test_the_tube_and_cap_are_gripped_at_thirty_five_percent():
    """Operator-specified grip width for both the tube and the cap.

    The fraction is authoritative; counts are derived from the gripper's 0..850 full scale.
    Both closes and the decap re-grip must agree, or the ratchet would re-close harder or
    looser than the initial grab.
    """
    assert handover.GRIP_FRACTION == pytest.approx(0.35)
    assert handover.GRIP_COUNTS == round(0.35 * handover.GRIPPER_FULL_SCALE_COUNTS) == 298

    plan = handover.build()
    closes = [a for a in plan if isinstance(a, ArmGripper) and a.state == "close"]
    assert {a.device for a in closes} == {"left", "right"}, "tube and cap"
    for a in closes:
        assert a.width == handover.GRIP_COUNTS, f"{a.device} closes at 35%"

    decap = next(a for a in plan if isinstance(a, ArmDecap))
    assert decap.grip_counts == handover.GRIP_COUNTS


def test_the_grip_width_is_inside_the_grippers_range():
    """A width outside 0..full-scale is rejected by the driver, not clamped — so a bad
    constant here would refuse to run rather than crush a tube, but it must not be bad."""
    assert 0 < handover.GRIP_COUNTS < handover.GRIPPER_FULL_SCALE_COUNTS


def test_opening_the_gripper_never_specifies_a_width():
    """`open` means fully open; a width on an open action would be ambiguous."""
    for a in handover.build():
        if isinstance(a, ArmGripper) and a.state == "open":
            assert a.width is None


def test_decap_is_a_full_turn_in_ninety_degree_bites():
    """R-ARM-5: 360° in 90° steps. The rewind between bites is the handler's invariant."""
    decap = next(a for a in handover.build() if isinstance(a, ArmDecap))
    assert decap.step_deg == 90.0
    assert decap.turns * 360.0 == pytest.approx(360.0)
    assert 360.0 / decap.step_deg == 4
    assert decap.device == "left"
    assert decap.speed == "slow"


def test_the_three_transitions_are_one_blended_traverse():
    """One traverse, not three moves — so the controller can blend instead of stopping."""
    trav = [a for a in handover.build() if isinstance(a, ArmTraverse)]
    assert len(trav) == 1
    assert trav[0].device == "right"
    assert trav[0].waypoints == ["TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE",
                                 "TRANSITION_LIQUID_HANDLER_TABLE"]
    assert trav[0].blend_deg and trav[0].blend_deg > 0


def test_speed_tiers_are_named_never_raw_numbers():
    """R-ENG-14: a raw mm/s in plan data would bypass the per-arm soft-limit clamp."""
    for a in _flat(handover.build()):
        assert a.speed in ("slow", "medium", "fast")


# --- the servo loop -----------------------------------------------------------

def test_the_servo_loop_terminates_on_the_remaining_offset():
    """Q4/D16: never on inter-view disagreement, which two views need never satisfy."""
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    assert loop.until == "offset_within_threshold"
    assert loop.watch_slot == "selected_offset"
    assert loop.threshold_mm == handover.OFFSET_THRESHOLD_MM


def test_the_servo_loop_is_bounded_in_both_ways():
    """A cap on iterations, and a no-progress abort — they catch different failures.

    Exhausting iterations means "needs longer"; no progress means the calibration is stale
    or wrong-signed, and more iterations will not help.
    """
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    assert 0 < loop.max_iterations <= handover.MAX_SERVO_ITERATIONS
    assert loop.no_progress_abort > 0


def test_the_servo_cameras_come_from_config_not_a_module_constant():
    """D19: never name the servo cameras in plan data.

    This bit once. A module-level tuple said `("handover_cam", "gripper_cam")` while config
    said `("handover_cam", "gripper_left_cam")` — and `gripper_cam` is the slot that looks
    across the room and detected **zero AprilTags in 53 sampled frames**. The plan would have
    written frames into a slot the solve never read, so the loop could not converge, and the
    entire cause was one identifier.

    Reading config at build time also means the bench can re-point a viewpoint without a code
    change, which is the point of R-CAM-6.
    """
    from core.config import settings

    assert handover.servo_cameras() == tuple(settings.servo_cameras)
    assert not hasattr(handover, "SERVO_CAMERAS"), (
        "a module-level constant re-introduces exactly the drift this replaced"
    )


def test_the_loop_only_looks_through_cameras_the_fleet_actually_has():
    """A servo camera absent from the fleet is a plan that cannot run, not a slow one."""
    from core.config import settings

    for cam in handover.servo_cameras():
        assert cam in settings.cameras, (
            f"{cam!r} is a servo camera but not a configured camera slot"
        )


def test_the_loop_body_looks_through_both_servo_cameras():
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    body = list(loop.body)
    snaps = [a for a in body if isinstance(a, CameraSnapshot)]
    assert {a.device for a in snaps} == set(handover.servo_cameras())
    for cam in handover.servo_cameras():
        targets = {a.target for a in body
                   if isinstance(a, VisionIdentify) and a.device == cam}
        assert targets == {"tip", "tube"}, f"{cam} must identify both tip and tube"


def test_every_loop_snapshot_demands_a_fresh_frame():
    """R-CAM-2/D20: a cached pre-move frame makes the loop re-command a correction it has
    already applied, and oscillate. Freshness is the correctness mechanism."""
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    snaps = [a for a in loop.body if isinstance(a, CameraSnapshot)]
    assert snaps and all(a.fresh for a in snaps)


def test_the_loop_solves_once_across_both_views_and_renders_overlays():
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    solves = [a for a in loop.body if isinstance(a, VisionSolveOffset)]
    assert len(solves) == 1, "one fused solve, not one per camera"
    assert solves[0].device is None, "device=None means every servo camera"
    assert solves[0].render_overlay is True


def test_the_loop_ends_by_moving_the_liquid_handler_from_the_solved_offset():
    """The nudge reads the blackboard: the numbers are computed, not planned."""
    loop = next(a for a in handover.build() if isinstance(a, Loop))
    nudge = list(loop.body)[-1]
    assert nudge.kind == "lh.move_relative"
    assert nudge.from_slot == "selected_offset"
    assert nudge.clamp_mm > 0, "one iteration's motion must be bounded (O9)"


def test_there_are_no_nested_loops():
    """D14: nested loops plus per-iteration materialization is a quadratic growth path."""
    top = [a for a in handover.build() if isinstance(a, Loop)]
    assert len(top) == 1
    assert not any(isinstance(b, Loop) for b in top[0].body)


def test_the_workflow_ends_by_retracting_the_liquid_handler_upward():
    last = handover.build()[-1]
    assert last.kind == "lh.move_relative"
    assert last.dz > 0, "positive dz is up"
    assert last.from_slot is None, "the retract is a fixed move, not a solved one"


# --- startup plan -------------------------------------------------------------

def test_the_startup_plan_initializes_everything_and_homes_slowly():
    plan = handover.build_startup()
    assert len(plan) == 1
    init = plan[0]
    assert init.kind == "lifecycle.initialize"
    assert init.device is None, "None means every device"
    assert init.home_after is True
    assert init.speed == "slow", "R-INIT-3: the home move is slow"


def test_homing_can_be_skipped():
    assert handover.build_startup(home_after=False)[0].home_after is False


# --- pre-flight coupling ------------------------------------------------------

def test_waypoints_used_is_derived_from_the_plan_not_hand_listed():
    """Adding a waypoint to the workflow must not need a second edit to be pre-flighted."""
    used = set(handover.waypoints_used())
    from_plan = set()
    for a in _flat(handover.build()):
        if isinstance(a, ArmWaypoint):
            from_plan.add((a.device, a.waypoint))
        elif isinstance(a, ArmTraverse):
            from_plan |= {(a.device, n) for n in a.waypoints}
    assert used == from_plan


def test_an_untaught_workflow_waypoint_is_reported_as_a_device_name_pair(isolated_teach_poses):
    """R-WP-5: bare names would leave the operator guessing which arm is untaught."""
    missing = waypoints.missing_for_plan(handover.waypoints_used())
    assert missing, "an empty library must report every waypoint as missing"
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in missing)
    assert set(missing) == set(handover.waypoints_used())
