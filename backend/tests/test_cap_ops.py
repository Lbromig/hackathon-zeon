"""The cap ratchet.

The sequence matters more than the code: turn gripped, open, unwind free, re-grip,
turn again. Getting the open/close the wrong way round would screw the cap back on
instead of off, and would do it confidently.
"""
from __future__ import annotations

import pytest

import drivers.mock  # noqa: F401
from core.motion import cap_ops
from drivers import build_driver


def _arm(joints=None, limits=None):
    cfg = {"type": "mock_arm", "id": "right"}
    if limits:
        cfg["limits"] = {"joints": limits}
    a = build_driver(cfg)
    a.connect()
    if joints:
        a.move_joints(joints)
    return a


def test_plan_matches_the_specified_ratchet():
    """+180, open, -180, close, +180, open, -180 — a full turn in two bites,
    ending released and back at the starting wrist angle."""
    assert cap_ops.plan_unscrew(2) == [
        ("turn", 180.0), ("open", 0.0), ("turn", -180.0),
        ("close", 0.0),
        ("turn", 180.0), ("open", 0.0), ("turn", -180.0),
    ]


def test_single_bite_still_opens_and_returns():
    assert cap_ops.plan_unscrew(1) == [("turn", 180.0), ("open", 0.0), ("turn", -180.0)]


def test_plan_ends_released_and_unwound():
    """The cap is left loose on the tube; lifting it away is a separate action."""
    for n in (1, 2, 3, 4):
        steps = cap_ops.plan_unscrew(n)
        assert steps[-1] == ("turn", -180.0)
        assert steps[-2] == ("open", 0.0)


def test_the_wrist_only_ever_unwinds_while_the_jaws_are_open():
    """A -180 with the cap held would screw it back on."""
    for n in (2, 3, 4):
        steps = cap_ops.plan_unscrew(n)
        holding = True                      # the routine starts gripped
        for action, degrees in steps:
            if action == "open":
                holding = False
            elif action == "close":
                holding = True
            elif degrees < 0:
                assert not holding, f"unwind while gripped in {steps}"


def test_cap_rotation_totals_180_per_bite():
    for n in (1, 2, 3, 4):
        gripped_turn = 0.0
        holding = True
        for action, degrees in cap_ops.plan_unscrew(n):
            if action == "open":
                holding = False
            elif action == "close":
                holding = True
            elif holding:
                gripped_turn += degrees
        assert gripped_turn == 180.0 * n


def test_net_wrist_travel_is_zero_so_the_routine_is_repeatable():
    """Without the closing unwind each call would walk J6 another 180 deg."""
    for n in (1, 2, 3, 4):
        net = sum(d for a, d in cap_ops.plan_unscrew(n) if a == "turn")
        assert net == 0.0


def test_zero_or_negative_bites_is_refused():
    with pytest.raises(cap_ops.CapOpError):
        cap_ops.plan_unscrew(0)


def test_unscrew_returns_every_joint_to_where_it_started():
    arm = _arm()
    before = arm.get_joints()
    cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert arm.get_joints() == pytest.approx(before), "the arm must end where it began"


def test_unscrew_leaves_the_cap_released():
    arm = _arm()
    cap_ops.grab_cap(arm)
    cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert arm.gripper_width() == pytest.approx(850.0), "jaws must end open"


def test_unscrewing_twice_does_not_accumulate_wrist_travel():
    arm = _arm()
    before = arm.get_joints()
    for _ in range(3):
        cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert arm.get_joints() == pytest.approx(before)


def test_unscrew_is_refused_up_front_when_it_would_exceed_the_joint_limit():
    """Refusing halfway would leave the cap part-unscrewed and the wrist wound."""
    limits = [[-360, 360]] * 5 + [[-360, 200]]      # J6 capped at 200
    arm = _arm(joints=[0, 0, 0, 0, 0, 100.0], limits=limits)
    with pytest.raises(cap_ops.CapOpError) as e:
        cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert "J6" in str(e.value)
    # and nothing moved
    assert arm.get_joints()[-1] == pytest.approx(100.0)


def test_grab_and_ungrab_drive_the_gripper():
    arm = _arm()
    cap_ops.ungrab_cap(arm)
    assert arm.gripper_width() == pytest.approx(850.0)
    cap_ops.grab_cap(arm)
    assert arm.gripper_width() == pytest.approx(0.0)
    cap_ops.grab_cap(arm, cap_ops.CapConfig(grip_counts=120.0))
    assert arm.gripper_width() == pytest.approx(120.0)


def test_unscrew_emits_a_step_callback_for_progress():
    arm = _arm()
    seen: list[str] = []
    cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0), on_step=seen.append)
    assert len(seen) == 7
    assert seen[0].startswith("turn +180")
    assert seen[-1].startswith("turn -180")
    assert "open" in seen and "close" in seen
