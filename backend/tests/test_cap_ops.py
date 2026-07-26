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
    """Refusing halfway would leave the cap part-unscrewed and the wrist wound.

    `auto_unwind=False` selects this behaviour explicitly. The default is now to rewind the
    wrist and proceed — see the recovery tests below.
    """
    limits = [[-360, 360]] * 5 + [[-360, 200]]      # J6 capped at 200
    arm = _arm(joints=[0, 0, 0, 0, 0, 100.0], limits=limits)
    with pytest.raises(cap_ops.CapOpError) as e:
        cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0,
                                                   auto_unwind=False))
    assert "J6" in str(e.value)
    # and nothing moved
    assert arm.get_joints()[-1] == pytest.approx(100.0)


# --- recovery: rewind the wrist instead of refusing ---------------------------------
#
# The bench hit this as "unscrew refused before starting: J6 would reach 484.3°". The wrist
# being left wound from a previous unscrew is the normal state, so the plan not fitting is a
# starting-position problem, not an impossible request.


def test_unscrew_rewinds_the_wrist_instead_of_refusing():
    """J6 at 304° with a 360° ceiling: a 180° bite would reach 484°, so rewind first."""
    limits = [[-360, 360]] * 5 + [[-360, 360]]
    arm = _arm(joints=[0, 0, 0, 0, 0, 304.0], limits=limits)

    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))

    # 304 + 180 overshoots 360 by 124, and the rewind adds UNWIND_MARGIN_DEG on top so a
    # real arm landing a few hundredths short still clears the limit — see
    # test_cap_ops_unwind_margin.py for the bench refusal that made the margin necessary.
    expected = 304.0 + 180.0 - (360.0 - cap_ops.UNWIND_MARGIN_DEG)
    assert result.unwound_deg == pytest.approx(expected)
    assert result.total_rotation_deg == pytest.approx(360.0), "cap still turns the full amount"
    # The wrist ends where the REWIND left it, not where it started: that is the point.
    assert arm.get_joints()[-1] == pytest.approx(304.0 - expected)
    assert result.net_wrist_travel_deg == pytest.approx(0.0), "no drift across the bites"


def test_the_rewind_happens_with_the_jaws_open_so_the_cap_does_not_turn():
    """Rewinding while gripped would screw the cap back down by exactly the rewind."""
    limits = [[-360, 360]] * 5 + [[-360, 360]]
    arm = _arm(joints=[0, 0, 0, 0, 0, 304.0], limits=limits)
    arm.grip()                                    # start gripped, as the ratchet requires
    order: list[str] = []
    real_release, real_grip = arm.release, arm.grip

    def note_release():
        order.append(f"release@{arm.get_joints()[-1]:.0f}")
        real_release()

    def note_grip(width=None):
        order.append(f"grip@{arm.get_joints()[-1]:.0f}")
        real_grip(width=width)

    arm.release, arm.grip = note_release, note_grip
    cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))

    # First two events: release at the wound angle, then re-grip lower by the whole rewind
    # (the 124° overshoot plus UNWIND_MARGIN_DEG). The wrist moved only while open, so the cap
    # never saw it — which is the property under test, not the particular angle.
    rewind = 304.0 + 180.0 - (360.0 - cap_ops.UNWIND_MARGIN_DEG)
    assert order[0] == "release@304", order
    assert order[1] == f"grip@{304.0 - rewind:.0f}", order


def test_no_rewind_when_the_plan_already_fits():
    arm = _arm(joints=[0, 0, 0, 0, 0, 0.0], limits=[[-360, 360]] * 6)
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert result.unwound_deg == 0.0
    assert arm.get_joints()[-1] == pytest.approx(0.0)


def test_rewind_is_refused_when_the_joint_range_cannot_fit_the_plan_at_all():
    """A bite bigger than the whole joint range is not a positioning problem."""
    limits = [[-360, 360]] * 5 + [[0, 90]]        # 90° of range, 180° bite
    arm = _arm(joints=[0, 0, 0, 0, 0, 45.0], limits=limits)
    with pytest.raises(cap_ops.CapOpError, match="no starting angle"):
        cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))


def test_a_rewind_that_fits_the_top_always_fits_the_bottom_for_a_ratchet():
    """Why `required_unwind`'s low-end guard cannot fire on a ratchet plan.

    A bite goes ``+step`` then ``-step``, so a ratchet never dips below the angle it started
    at: its excursion trough is 0. The rewind therefore leaves the low point at ``hi - peak``,
    and any plan for which that is below ``lo`` has ``peak > hi - lo`` — which the
    range check has already refused. The guard stays as defence for a future plan shape that
    *does* dip below its start; this test is why nobody should expect to trigger it today.
    """
    assert cap_ops.plan_excursion(cap_ops.plan_unscrew(2))[1] == 0.0

    limits = [[-360, 360]] * 5 + [[100, 300]]     # 200° of range, 180° bite, parked high
    arm = _arm(joints=[0, 0, 0, 0, 0, 290.0], limits=limits)
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    expected = 290.0 + 180.0 - (300.0 - cap_ops.UNWIND_MARGIN_DEG)
    assert result.unwound_deg == pytest.approx(expected), "290 + 180 - (300 - margin)"
    landed = 290.0 - expected
    assert arm.get_joints()[-1] == pytest.approx(landed)
    assert 100.0 + cap_ops.UNWIND_MARGIN_DEG <= landed <= 300.0, "inside [100, 300] with margin"


def test_unscrew_sentence_reports_the_rewind():
    """Real motion the operator did not ask for must appear in the result."""
    arm = _arm(joints=[0, 0, 0, 0, 0, 304.0], limits=[[-360, 360]] * 6)
    sentence = cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert f"rewound wrist {304.0 + 180.0 - (360.0 - cap_ops.UNWIND_MARGIN_DEG):.0f}" in sentence
    assert "jaws open" in sentence


# --- the generalized bite (D12 / R-ARM-5) -------------------------------------------
#
# `arm.decap` wants 360° in 90° bites. Everything below asserts that this is the *same*
# plan with a different bite size, and that both invariants survive the generalization.

def test_ninety_degree_bites_take_four_of_them_to_turn_the_cap_once():
    steps = cap_ops.plan_ratchet(90.0, 360.0)
    assert steps == [
        ("turn", 90.0), ("open", 0.0), ("turn", -90.0), ("close", 0.0),
        ("turn", 90.0), ("open", 0.0), ("turn", -90.0), ("close", 0.0),
        ("turn", 90.0), ("open", 0.0), ("turn", -90.0), ("close", 0.0),
        ("turn", 90.0), ("open", 0.0), ("turn", -90.0),
    ]
    assert cap_ops.count_bites(steps) == 4


def test_the_generalized_plan_keeps_both_invariants_at_every_bite_size():
    """Net-zero wrist travel and a cap rotation equal to the request, for any bite."""
    for step_deg in (30.0, 45.0, 90.0, 120.0, 180.0):
        for total in (90.0, 360.0, 720.0):
            steps = cap_ops.plan_ratchet(step_deg, total)
            assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0), (step_deg, total)
            assert cap_ops.gripped_rotation(steps) == pytest.approx(total), (step_deg, total)


def test_the_wrist_never_exceeds_one_bite_which_is_why_90_preflights_more_easily():
    """Peak excursion is exactly one bite, so halving the bite halves what a soft limit
    has to accept. That is the whole reason the decap default is 90 and not 180."""
    for step_deg in (45.0, 90.0, 180.0):
        angle, peak = 0.0, 0.0
        for action, degrees in cap_ops.plan_ratchet(step_deg, 360.0):
            if action == "turn":
                angle += degrees
                peak = max(peak, abs(angle))
        assert peak == pytest.approx(step_deg)


def test_the_wrist_still_only_unwinds_with_the_jaws_open_in_90_degree_bites():
    holding = True
    for action, degrees in cap_ops.plan_ratchet(90.0, 360.0):
        if action == "open":
            holding = False
        elif action == "close":
            holding = True
        elif degrees < 0:
            assert not holding, "unwind while gripped would screw the cap back on"


def test_a_total_that_is_not_a_whole_number_of_bites_gets_a_short_last_bite():
    """Neither rounded up (over-turning the cap) nor down (leaving it tight)."""
    assert cap_ops.bite_sizes(90.0, 200.0) == pytest.approx([90.0, 90.0, 20.0])
    steps = cap_ops.plan_ratchet(90.0, 200.0)
    assert cap_ops.gripped_rotation(steps) == pytest.approx(200.0)
    assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0)
    assert steps[-1] == ("turn", -20.0)


def test_no_float_dust_bite_when_the_total_divides_exactly():
    assert len(cap_ops.bite_sizes(90.0, 360.0)) == 4
    assert len(cap_ops.bite_sizes(120.0, 360.0)) == 3


def test_a_nonsense_bite_or_total_is_refused():
    for bad in (0.0, -90.0):
        with pytest.raises(cap_ops.CapOpError):
            cap_ops.plan_ratchet(bad, 360.0)
        with pytest.raises(cap_ops.CapOpError):
            cap_ops.plan_ratchet(90.0, bad)


def test_the_legacy_180_spelling_is_the_generalized_plan():
    """`plan_unscrew` must stay exactly what the teach tab's /cap button gets."""
    for n in (1, 2, 3, 4):
        assert cap_ops.plan_unscrew(n) == cap_ops.plan_ratchet(180.0, 180.0 * n)


def test_config_selects_the_form_and_never_both_at_once():
    assert cap_ops.CapConfig().bite_deg == 180.0            # legacy default
    assert cap_ops.CapConfig().total_deg == 360.0
    assert cap_ops.CapConfig(step_deg=90.0).total_deg == 360.0
    assert cap_ops.CapConfig(step_deg=90.0).bite_deg == 90.0
    assert cap_ops.CapConfig(turns=2.0).bite_deg == 90.0    # step defaults to the decap bite
    assert cap_ops.CapConfig(turns=2.0).total_deg == 720.0
    # half_turns is ignored once the general form is selected, so a caller cannot express
    # two different plans and have one of them silently win.
    cfg = cap_ops.CapConfig(half_turns=4, step_deg=90.0)
    assert cfg.total_deg == 360.0 and cap_ops.count_bites(cfg.plan()) == 4


def test_a_90_degree_decap_runs_four_bites_and_returns_the_wrist():
    arm = _arm()
    before = arm.get_joints()
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0))
    assert result.bites == 4
    assert result.step_deg == 90.0
    assert result.total_rotation_deg == pytest.approx(360.0)
    assert result.net_wrist_travel_deg == pytest.approx(0.0)
    assert result.returned and result.preflight_ok
    assert arm.get_joints() == pytest.approx(before)
    assert arm.gripper_width() == pytest.approx(850.0), "cap must be left released"


def test_on_bite_fires_between_bites_with_the_jaws_open_and_the_wrist_home():
    """The engine's checkpoint hangs off this callback, so the state it sees matters."""
    arm = _arm()
    seen: list[tuple[int, int, float]] = []

    def on_bite(n: int, total: int, rotated: float) -> None:
        seen.append((n, total, rotated))
        assert arm.gripper_width() == pytest.approx(850.0), "jaws must be open at a pause"
        assert arm.get_joints()[-1] == pytest.approx(0.0), "wrist must be home at a pause"

    cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0), on_bite=on_bite)
    assert seen == [(1, 4, 90.0), (2, 4, 180.0), (3, 4, 270.0), (4, 4, 360.0)]


def test_the_90_degree_ratchet_is_refused_up_front_when_a_bite_would_exceed_the_limit():
    """With auto_unwind off, the pre-flight still refuses rather than starting."""
    limits = [[-360, 360]] * 5 + [[-360, 150]]       # J6 capped at 150
    arm = _arm(joints=[0, 0, 0, 0, 0, 100.0], limits=limits)
    with pytest.raises(cap_ops.CapOpError) as e:
        cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0,
                                                  auto_unwind=False))
    assert "J6" in str(e.value)
    assert arm.get_joints()[-1] == pytest.approx(100.0), "nothing may have moved"


def test_a_90_degree_bite_is_accepted_where_a_180_degree_bite_is_refused():
    """The concrete payoff of the smaller bite: the same arm can still decap.

    J6 parked at 100° with a 200° ceiling. A 180° bite would reach 280° and is refused; a
    90° bite peaks at 190° and turns the cap the same 360° in four goes.
    """
    limits = [[-360, 360]] * 5 + [[-360, 200]]
    joints = [0, 0, 0, 0, 0, 100.0]
    with pytest.raises(cap_ops.CapOpError):
        cap_ops.run_ratchet(_arm(joints=joints, limits=limits),
                            cap_ops.CapConfig(half_turns=2, settle_s=0.0,
                                              auto_unwind=False))
    result = cap_ops.run_ratchet(_arm(joints=joints, limits=limits),
                                 cap_ops.CapConfig(step_deg=90.0, settle_s=0.0,
                                                   auto_unwind=False))
    assert result.bites == 4
    assert result.total_rotation_deg == pytest.approx(360.0)
    assert result.net_wrist_travel_deg == pytest.approx(0.0)


def test_repeating_a_90_degree_decap_does_not_walk_the_wrist():
    arm = _arm()
    before = arm.get_joints()
    for _ in range(3):
        cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0))
    assert arm.get_joints() == pytest.approx(before)


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
    # 7 plan steps plus one lift, which is reported because it is real arm motion.
    assert len(seen) == 8
    assert seen[0].startswith("turn +180")
    assert seen[-1].startswith("turn -180")
    assert "open" in seen and "close" in seen
    assert any("lift" in s for s in seen)


# --- following the cap up its thread ------------------------------------------------


def test_the_arm_lifts_at_every_regrip():
    """A cap backing off rises; the jaws must come up with it or fight the thread."""
    arm = _arm()
    z_before = arm.get_pose().z
    # 90° bites => 4 bites => 3 re-grips => 3 lifts.
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0))

    assert result.bites == 4
    assert result.lifted_mm == pytest.approx(6.0), "3 re-grips x 2 mm"
    assert arm.get_pose().z == pytest.approx(z_before + 6.0)
    assert result.net_wrist_travel_deg == pytest.approx(0.0), "the wrist still returns"


def test_the_lift_happens_while_the_jaws_are_open():
    """Lifting while gripping would drag the cap up the thread instead of following it."""
    arm = _arm()
    arm.grip()
    events: list[str] = []
    real_grip, real_release, real_move = arm.grip, arm.release, arm.move_relative

    def note_grip(width=None):
        events.append("grip")
        real_grip(width=width)

    def note_release():
        events.append("release")
        real_release()

    def note_move(**kw):
        if kw.get("dz"):
            events.append(f"lift{kw['dz']:+g}")
        real_move(**kw)

    arm.grip, arm.release, arm.move_relative = note_grip, note_release, note_move
    cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))

    # Every lift must be preceded by a release and followed by a grip: jaws open throughout.
    for i, e in enumerate(events):
        if e.startswith("lift"):
            assert "release" in events[:i], f"lift before any release: {events}"
            assert events[i + 1] == "grip", f"lift not immediately before a grip: {events}"
    assert any(e.startswith("lift") for e in events), events


def test_lift_can_be_switched_off_and_then_the_arm_returns_exactly():
    """The original invariant — whole arm back where it started — with lifting disabled."""
    arm = _arm()
    before = arm.get_joints()
    result = cap_ops.run_ratchet(
        arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0, lift_per_regrip_mm=0.0))
    assert result.lifted_mm == 0.0
    assert arm.get_joints() == pytest.approx(before)


def test_a_single_bite_never_regrips_so_it_never_lifts():
    arm = _arm()
    z_before = arm.get_pose().z
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=1, settle_s=0.0))
    assert result.bites == 1
    assert result.lifted_mm == 0.0
    assert arm.get_pose().z == pytest.approx(z_before)


def test_unscrew_sentence_reports_the_lift():
    arm = _arm()
    sentence = cap_ops.unscrew_cap(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0))
    assert "raised 6 mm" in sentence
