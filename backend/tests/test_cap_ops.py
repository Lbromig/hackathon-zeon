"""The cap ratchet.

The sequence matters more than the code: turn gripped, open, unwind free, re-grip,
turn again. Getting the open/close the wrong way round would screw the cap back on
instead of off, and would do it confidently.

Which *way* a gripped turn goes is bench configuration, not a property of the ratchet:
`cap_ops.UNSCREW_SIGN` says whether loosening is +J6 or -J6, and that depends on how the
gripper happens to be bolted to the flange. The bench found the old hardcoded positive turn
*tightening* the cap, so nothing here may hardcode a direction — a test that asserts `+90`
is a test that goes wrong the next time a gripper is re-mounted. Everything below therefore
derives its expectations from `SIGN`, and the assertions are about the shape of the plan:
gripped turns go one way, jaws-open unwinds come back the other, net travel is zero.

The same goes for what the routine does with the cap when it finishes. `cap_ops.END_GRIPPED`
says whether the ratchet ends **holding** the loosened cap or leaves it sitting on the tube;
the bench asked for holding, because the arm's next move lifts the cap away and it cannot do
that with open jaws. That is a contract knob, not an invariant, so nothing here spells a step
count or a final jaw state either — expectations come from `END_GRIPPED`/`FINAL_GRIP`, and
both settings get their own test so neither path rots. What *is* invariant survives both:
the cap only turns gripped, the wrist only unwinds open, net travel is zero, and the closing
grip comes strictly after the last unwind.
"""
from __future__ import annotations

import pytest

import drivers.mock  # noqa: F401
from core.motion import cap_ops
from drivers import build_driver

#: Which way the tool axis turns to loosen (-1 or +1). Read from the module rather than
#: restated, so flipping it for a re-mounted gripper mirrors this suite instead of breaking it.
SIGN = cap_ops.UNSCREW_SIGN
MARGIN = cap_ops.UNWIND_MARGIN_DEG
TOL = cap_ops.PREFLIGHT_TOLERANCE_DEG
#: Whether the ratchet finishes holding the cap. Read, not restated, for the same reason.
END_GRIPPED = cap_ops.END_GRIPPED
#: The tail the contract adds: a closing grip on the loosened cap, or nothing at all.
FINAL_GRIP: list[cap_ops.Step] = [("close", 0.0)] if END_GRIPPED else []

#: The mock parallel gripper's fully-open width, in counts.
OPEN_COUNTS = 850.0


def _closes(steps: list[cap_ops.Step]) -> int:
    """How many times a plan closes the jaws — one lift each, and the last one may be the
    contract's final grip rather than a re-grip between bites."""
    return sum(1 for action, _ in steps if action == "close")


def _final_jaw(steps: list[cap_ops.Step]) -> str:
    """The last thing a plan does to the jaws: ``"close"`` or ``"open"``."""
    return next(action for action, _ in reversed(steps) if action in ("open", "close"))


def _resting_width(grip_counts: float | None = None) -> float:
    """The gripper width the ratchet leaves behind, for the configured end state."""
    if not END_GRIPPED:
        return OPEN_COUNTS
    return 0.0 if grip_counts is None else grip_counts


def _toward(deg: float) -> float:
    """``deg`` degrees in the loosening direction, signed."""
    return SIGN * deg


def _gripped(deg: float) -> float:
    """The signed turn of one ``deg``-sized bite taken *with the cap held*."""
    return SIGN * deg


def _unwind(deg: float) -> float:
    """The signed jaws-open turn that brings the wrist back from a ``deg`` bite."""
    return -SIGN * deg


def _is_unwind(degrees: float) -> bool:
    """True for a turn that runs against the loosening direction — a wrist unwind."""
    return degrees * SIGN < 0


def _j6_limits(bound: float, far: float = 360.0) -> list[list[float]]:
    """J6 soft limits with the binding one ``bound``° away in the loosening direction.

    The plan runs toward exactly one limit, and which one flips with `SIGN`. Building the
    limits from the direction rather than writing `[-360, 200]` is what keeps "the wrist runs
    out of room" the subject of a test instead of "the upper limit is 200".
    """
    near, other = _toward(bound), -_toward(far)
    return [[-360.0, 360.0]] * 5 + [[min(near, other), max(near, other)]]


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
    """One bite gripped, open, the same bite back free, close, again — a full turn in two
    bites, back at the starting wrist angle, then whatever `END_GRIPPED` asks for. The gripped
    turn goes the loosening way and the unwind comes back; which way that is, is
    `UNSCREW_SIGN`'s business.
    """
    assert cap_ops.plan_unscrew(2) == [
        ("turn", _gripped(180.0)), ("open", 0.0), ("turn", _unwind(180.0)),
        ("close", 0.0),
        ("turn", _gripped(180.0)), ("open", 0.0), ("turn", _unwind(180.0)),
    ] + FINAL_GRIP


def test_single_bite_still_opens_and_returns():
    assert cap_ops.plan_unscrew(1) == [
        ("turn", _gripped(180.0)), ("open", 0.0), ("turn", _unwind(180.0))] + FINAL_GRIP


def test_the_direction_of_a_gripped_turn_is_configuration_not_a_constant():
    """The defect the bench reported: with the turn hardcoded, a gripper mounted the other way
    round makes the routine *tighten* a seated cap with the full joint torque behind it. Both
    directions must therefore be expressible, and asking for the other one must mirror the
    plan exactly rather than change its shape."""
    loosen = cap_ops.plan_ratchet(90.0, 360.0, -1.0)
    tighten = cap_ops.plan_ratchet(90.0, 360.0, +1.0)
    assert loosen == [(a, -d) for a, d in tighten]
    assert cap_ops.plan_ratchet(90.0, 360.0) == (loosen if SIGN < 0 else tighten), (
        "the default must be UNSCREW_SIGN, not whichever sign the code happens to write")
    # And the config threads it through, for both spellings of "how far".
    assert cap_ops.CapConfig().unscrew_sign == SIGN
    assert cap_ops.CapConfig(step_deg=90.0, unscrew_sign=+1.0).plan() == tighten
    assert cap_ops.CapConfig(half_turns=2, unscrew_sign=+1.0).plan() == \
        cap_ops.plan_ratchet(180.0, 360.0, +1.0)


def test_the_plan_ends_unwound_and_in_the_configured_jaw_state():
    """The wrist always comes home last; what the jaws do after that is `END_GRIPPED`.

    The unwind itself must be the final *turn* and must be preceded by the release — a wrist
    that came back gripped would have screwed the cap straight back down. Whether a closing
    grip then follows is the contract, not the invariant, so it is read off the module.
    """
    for n in (1, 2, 3, 4):
        steps = cap_ops.plan_unscrew(n)
        turns = [i for i, (action, _) in enumerate(steps) if action == "turn"]
        assert steps[turns[-1]] == ("turn", _unwind(180.0))
        assert steps[turns[-1] - 1] == ("open", 0.0)
        assert _final_jaw(steps) == ("close" if END_GRIPPED else "open")
        assert steps[turns[-1] + 1:] == FINAL_GRIP, "nothing may follow but the closing grip"


def test_ending_gripped_only_appends_a_close_after_the_wrist_is_home():
    """The bench fix, as a difference between two plans.

    "When retracting after the decap, the gripper opened first, so it didn't hold on to the
    decapped cap" — the last bite was turn / open / unwind and the routine finished with the
    jaws open. Holding the cap must therefore cost nothing but a trailing close: the same
    turns, in the same order, with the grip taken *after* the final unwind. Closing any
    earlier would screw the cap back down by exactly that unwind.
    """
    for step_deg, total in ((180.0, 360.0), (90.0, 360.0), (90.0, 200.0)):
        released = cap_ops.plan_ratchet(step_deg, total, SIGN, end_gripped=False)
        held = cap_ops.plan_ratchet(step_deg, total, SIGN, end_gripped=True)
        where = (step_deg, total)
        assert held == released + [("close", 0.0)], where
        assert _final_jaw(released) == "open", where
        assert _final_jaw(held) == "close", where
        # Neither invariant notices the difference.
        for steps in (released, held):
            assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0), where
            assert cap_ops.gripped_rotation(steps) == pytest.approx(_toward(total)), where
        # And the default is the module's contract rather than whichever the code writes.
        assert cap_ops.plan_ratchet(step_deg, total) == (held if END_GRIPPED else released)


def test_the_config_threads_the_end_state_through_both_spellings():
    """`CapConfig.end_gripped` is what gets the teach tab's /cap button the same contract."""
    assert cap_ops.CapConfig().end_gripped == END_GRIPPED
    for end_gripped in (True, False):
        assert cap_ops.CapConfig(step_deg=90.0, end_gripped=end_gripped).plan() == \
            cap_ops.plan_ratchet(90.0, 360.0, SIGN, end_gripped)
        assert cap_ops.CapConfig(half_turns=2, end_gripped=end_gripped).plan() == \
            cap_ops.plan_unscrew(2, SIGN, end_gripped)


def test_the_wrist_only_ever_unwinds_while_the_jaws_are_open():
    """An unwind with the cap held would screw it straight back on."""
    for n in (2, 3, 4):
        steps = cap_ops.plan_unscrew(n)
        holding = True                      # the routine starts gripped
        for action, degrees in steps:
            if action == "open":
                holding = False
            elif action == "close":
                holding = True
            elif _is_unwind(degrees):
                assert not holding, f"unwind while gripped in {steps}"
            else:
                assert holding, f"the cap was turned with the jaws open in {steps}"


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
        # Signed, because the cap turns in joint space: the magnitude is the request and the
        # sign is the loosening direction.
        assert gripped_turn == _gripped(180.0 * n)
        assert abs(gripped_turn) == 180.0 * n


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


def test_unscrew_leaves_the_cap_in_the_configured_state():
    arm = _arm()
    cap_ops.grab_cap(arm)
    cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert arm.gripper_width() == pytest.approx(_resting_width()), \
        f"jaws must end {'closed on the cap' if END_GRIPPED else 'open'}"


def test_a_run_that_ends_gripped_really_is_still_holding_the_cap():
    """The point of the change: the caller's next move lifts the cap, and a lift with open
    jaws carries nothing. Asserted against the arm, not against the plan."""
    arm = _arm()
    cap_ops.grab_cap(arm, cap_ops.CapConfig(grip_counts=298.0))
    result = cap_ops.run_ratchet(
        arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0, grip_counts=298.0,
                               end_gripped=True))
    assert result.ended_gripped is True
    assert arm.gripper_width() == pytest.approx(298.0), "the jaws must still be on the cap"
    assert result.net_wrist_travel_deg == pytest.approx(0.0), \
        "and the wrist still came home — the grip is taken after the unwind, not instead of it"


def test_a_run_that_ends_released_leaves_the_cap_on_the_tube():
    """The other contract, kept alive so `end_gripped=False` cannot rot into a dead branch."""
    arm = _arm()
    cap_ops.grab_cap(arm, cap_ops.CapConfig(grip_counts=298.0))
    result = cap_ops.run_ratchet(
        arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0, grip_counts=298.0,
                               end_gripped=False))
    assert result.ended_gripped is False
    assert arm.gripper_width() == pytest.approx(OPEN_COUNTS), "the cap was let go"
    assert result.net_wrist_travel_deg == pytest.approx(0.0)


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
    # 100° of room in the loosening direction, and one bite needs 180.
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(100.0)], limits=_j6_limits(200.0))
    with pytest.raises(cap_ops.CapOpError) as e:
        cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0,
                                                   auto_unwind=False))
    assert "J6" in str(e.value)
    # and nothing moved
    assert arm.get_joints()[-1] == pytest.approx(_toward(100.0))


# --- recovery: rewind the wrist instead of refusing ---------------------------------
#
# The bench hit this as "unscrew refused before starting: J6 would reach 484.3°". The wrist
# being left wound from a previous unscrew is the normal state, so the plan not fitting is a
# starting-position problem, not an impossible request.


#: A wrist parked 304° round in the loosening direction, unscrewing in 180° bites against a
#: 360° soft limit: the bite would reach 484°, overshooting by 124°, and the rewind takes
#: UNWIND_MARGIN_DEG on top so a real arm landing a few hundredths short still clears the
#: limit — see test_cap_ops_unwind_margin.py for the bench refusal that made the margin
#: necessary. A magnitude; `unwound_deg` carries the sign.
PARKED_DEG = 304.0
REWIND_DEG = PARKED_DEG + 180.0 - (360.0 - MARGIN)


def test_unscrew_rewinds_the_wrist_instead_of_refusing():
    """A 180° bite from 304° round would reach 484° past a 360° limit, so rewind first."""
    limits = [[-360, 360]] * 5 + [[-360, 360]]
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(PARKED_DEG)], limits=limits)

    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))

    # `unwound_deg` keeps `required_unwind`'s convention: positive means "rotate the tool axis
    # negative by this much", so it runs *against* the loosening direction. The magnitude is
    # the interesting part, and the landed angle is the check that it went the right way.
    assert abs(result.unwound_deg) == pytest.approx(REWIND_DEG)
    assert result.unwound_deg == pytest.approx(_toward(REWIND_DEG))
    assert result.total_rotation_deg == pytest.approx(_gripped(360.0)), \
        "cap still turns the full amount"
    # The wrist ends where the REWIND left it, not where it started: that is the point.
    assert arm.get_joints()[-1] == pytest.approx(_toward(PARKED_DEG - REWIND_DEG))
    assert result.net_wrist_travel_deg == pytest.approx(0.0), "no drift across the bites"


def test_the_rewind_happens_with_the_jaws_open_so_the_cap_does_not_turn():
    """Rewinding while gripped would screw the cap back down by exactly the rewind."""
    limits = [[-360, 360]] * 5 + [[-360, 360]]
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(PARKED_DEG)], limits=limits)
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

    # First two events: release at the wound angle, then re-grip a whole rewind back toward
    # zero (the 124° overshoot plus UNWIND_MARGIN_DEG). The wrist moved only while open, so
    # the cap never saw it — which is the property under test, not the particular angle.
    assert order[0] == f"release@{_toward(PARKED_DEG):.0f}", order
    assert order[1] == f"grip@{_toward(PARKED_DEG - REWIND_DEG):.0f}", order


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


def test_a_rewind_that_fits_one_limit_always_fits_the_other_for_a_ratchet():
    """Why `required_unwind`'s far-side guard cannot fire on a ratchet plan.

    A bite goes one bite out and the same bite back, so a ratchet never travels past its
    starting angle in the *other* direction: one end of its excursion is exactly 0, and which
    end that is flips with `UNSCREW_SIGN`. The rewind therefore leaves the far end one bite
    inside the far limit, and any plan for which that is outside it needs more than the whole
    joint range — which the range check has already refused. The guard stays as defence for a
    future plan shape that straddles its start; this test is why nobody should expect to
    trigger it today.
    """
    peak, trough = cap_ops.plan_excursion(cap_ops.plan_unscrew(2))
    assert (trough if SIGN < 0 else peak) == pytest.approx(_gripped(180.0)), "one bite out"
    assert (peak if SIGN < 0 else trough) == 0.0, "and never the other way"

    # 200° of range, a 180° bite, parked 10° short of the limit the plan runs toward.
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(290.0)], limits=_j6_limits(300.0, 100.0))
    result = cap_ops.run_ratchet(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    expected = 290.0 + 180.0 - (300.0 - MARGIN)
    assert abs(result.unwound_deg) == pytest.approx(expected), "290 + 180 - (300 - margin)"
    landed = arm.get_joints()[-1]
    assert landed == pytest.approx(_toward(290.0 - expected))
    lo, hi = arm.limits.joints[5]
    assert lo + MARGIN <= landed <= hi - MARGIN, f"inside [{lo}, {hi}] with margin"


def test_unscrew_sentence_reports_the_rewind():
    """Real motion the operator did not ask for must appear in the result."""
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(PARKED_DEG)], limits=[[-360, 360]] * 6)
    sentence = cap_ops.unscrew_cap(arm, cap_ops.CapConfig(half_turns=2, settle_s=0.0))
    assert "rewound wrist" in sentence
    assert f"{REWIND_DEG:.0f}" in sentence, sentence
    assert "jaws open" in sentence


# --- the generalized bite (D12 / R-ARM-5) -------------------------------------------
#
# `arm.decap` wants 360° in 90° bites. Everything below asserts that this is the *same*
# plan with a different bite size, and that both invariants survive the generalization.

def test_ninety_degree_bites_take_four_of_them_to_turn_the_cap_once():
    steps = cap_ops.plan_ratchet(90.0, 360.0)
    bite = [("turn", _gripped(90.0)), ("open", 0.0), ("turn", _unwind(90.0))]
    assert steps == bite + [("close", 0.0)] + bite + [("close", 0.0)] + \
        bite + [("close", 0.0)] + bite + FINAL_GRIP
    assert cap_ops.count_bites(steps) == 4


def test_the_generalized_plan_keeps_both_invariants_at_every_bite_size():
    """Net-zero wrist travel and a cap rotation equal to the request, for any bite —
    and for either loosening direction, since neither invariant is about the sign."""
    for sign in (-1.0, +1.0):
        for step_deg in (30.0, 45.0, 90.0, 120.0, 180.0):
            for total in (90.0, 360.0, 720.0):
                where = (sign, step_deg, total)
                steps = cap_ops.plan_ratchet(step_deg, total, sign)
                assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0), where
                assert cap_ops.gripped_rotation(steps) == pytest.approx(sign * total), where


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
        elif _is_unwind(degrees):
            assert not holding, "unwind while gripped would screw the cap back on"
        else:
            assert holding, "a turn with the jaws open turns no cap"


def test_a_total_that_is_not_a_whole_number_of_bites_gets_a_short_last_bite():
    """Neither rounded up (over-turning the cap) nor down (leaving it tight)."""
    assert cap_ops.bite_sizes(90.0, 200.0) == pytest.approx([90.0, 90.0, 20.0])
    steps = cap_ops.plan_ratchet(90.0, 200.0)
    assert cap_ops.gripped_rotation(steps) == pytest.approx(_gripped(200.0))
    assert cap_ops.net_wrist_travel(steps) == pytest.approx(0.0)
    # The short bite is unwound like any other; only the contract's closing grip may follow.
    assert [s for s in steps if s[0] == "turn"][-1] == ("turn", _unwind(20.0))
    assert steps[-1] == (FINAL_GRIP or [("turn", _unwind(20.0))])[-1]


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
    """`plan_unscrew` must stay exactly what the teach tab's /cap button gets — including the
    direction. The legacy spelling is a bite size, not a second opinion about which way a
    thread runs, so it has to go through the same sign as `arm.decap`."""
    for n in (1, 2, 3, 4):
        assert cap_ops.plan_unscrew(n) == cap_ops.plan_ratchet(180.0, 180.0 * n)
        for sign in (-1.0, +1.0):
            assert cap_ops.plan_unscrew(n, sign) == \
                cap_ops.plan_ratchet(180.0, 180.0 * n, sign)


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
    assert result.total_rotation_deg == pytest.approx(_gripped(360.0))
    assert result.net_wrist_travel_deg == pytest.approx(0.0)
    assert result.returned and result.preflight_ok
    assert arm.get_joints() == pytest.approx(before)
    assert result.ended_gripped is END_GRIPPED
    assert arm.gripper_width() == pytest.approx(_resting_width())


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
    # 50° of room in the loosening direction, and one bite needs 90.
    arm = _arm(joints=[0, 0, 0, 0, 0, _toward(100.0)], limits=_j6_limits(150.0))
    with pytest.raises(cap_ops.CapOpError) as e:
        cap_ops.run_ratchet(arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0,
                                                  auto_unwind=False))
    assert "J6" in str(e.value)
    assert arm.get_joints()[-1] == pytest.approx(_toward(100.0)), "nothing may have moved"


def test_a_90_degree_bite_is_accepted_where_a_180_degree_bite_is_refused():
    """The concrete payoff of the smaller bite: the same arm can still decap.

    J6 parked 100° round with the limit it unscrews toward at 200°. A 180° bite would reach
    280° and is refused; a 90° bite peaks at 190° and turns the cap the same 360° in four goes.
    """
    limits = _j6_limits(200.0)
    joints = [0, 0, 0, 0, 0, _toward(100.0)]
    with pytest.raises(cap_ops.CapOpError):
        cap_ops.run_ratchet(_arm(joints=joints, limits=limits),
                            cap_ops.CapConfig(half_turns=2, settle_s=0.0,
                                              auto_unwind=False))
    result = cap_ops.run_ratchet(_arm(joints=joints, limits=limits),
                                 cap_ops.CapConfig(step_deg=90.0, settle_s=0.0,
                                                   auto_unwind=False))
    assert result.bites == 4
    assert result.total_rotation_deg == pytest.approx(_gripped(360.0))
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
    cfg = cap_ops.CapConfig(half_turns=2, settle_s=0.0)
    plan = cfg.plan()
    seen: list[str] = []
    cap_ops.unscrew_cap(arm, cfg, on_step=seen.append)
    # One report per plan step, plus one per close: the lift that precedes a re-grip is real
    # arm motion and is reported as its own line. Derived from the plan, so the contract's
    # trailing grip (and the lift in front of it) does not turn this into arithmetic upkeep.
    assert len(seen) == len(plan) + _closes(plan), seen
    assert seen[0].startswith(f"turn {_gripped(180.0):+.0f}"), seen
    assert seen[-1] == ("close" if END_GRIPPED else f"turn {_unwind(180.0):+.0f}°"), seen
    assert "open" in seen and "close" in seen
    assert any("lift" in s for s in seen)


# --- following the cap up its thread ------------------------------------------------


def test_the_arm_lifts_at_every_regrip():
    """A cap backing off rises; the jaws must come up with it or fight the thread."""
    arm = _arm()
    z_before = arm.get_pose().z
    cfg = cap_ops.CapConfig(step_deg=90.0, settle_s=0.0)
    plan = cfg.plan()
    # One lift per close: 90° bites => 4 bites => 3 re-grips between them, plus the closing
    # grip when the routine ends holding the cap. Counted off the plan rather than written
    # down, because which of those exist is the contract's business.
    expected = _closes(plan) * cfg.lift_per_regrip_mm
    result = cap_ops.run_ratchet(arm, cfg)

    assert result.bites == 4
    assert result.lifted_mm == pytest.approx(expected), \
        f"{_closes(plan)} closes x {cfg.lift_per_regrip_mm:g} mm"
    assert arm.get_pose().z == pytest.approx(z_before + expected)
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


def test_a_single_bite_never_regrips_between_bites_so_it_only_lifts_to_take_hold():
    """There is no *inter-bite* re-grip in a one-bite plan, so the only lift a single bite can
    make is the one in front of the contract's closing grip — and with `end_gripped` off there
    is no lift at all. Both spellings are asserted, so neither depends on the other."""
    for end_gripped in (True, False):
        arm = _arm()
        z_before = arm.get_pose().z
        cfg = cap_ops.CapConfig(half_turns=1, settle_s=0.0, end_gripped=end_gripped)
        expected = (cfg.lift_per_regrip_mm if end_gripped else 0.0)
        result = cap_ops.run_ratchet(arm, cfg)
        assert result.bites == 1
        assert _closes(cfg.plan()) == (1 if end_gripped else 0), "no re-grip between bites"
        assert result.lifted_mm == pytest.approx(expected)
        assert arm.get_pose().z == pytest.approx(z_before + expected)


def test_unscrew_sentence_reports_the_lift():
    arm = _arm()
    cfg = cap_ops.CapConfig(step_deg=90.0, settle_s=0.0)
    lifted = _closes(cfg.plan()) * cfg.lift_per_regrip_mm
    sentence = cap_ops.unscrew_cap(arm, cfg)
    assert f"raised {lifted:.0f} mm" in sentence, sentence


def test_unscrew_sentence_says_whether_the_cap_is_still_held():
    """A caller reading the result has to be able to tell whether the arm can now lift the
    cap. "Cap released" when it is in fact held (or the reverse) is the bench report again,
    only in prose."""
    for end_gripped, expected in ((True, "cap held"), (False, "cap released")):
        arm = _arm()
        sentence = cap_ops.unscrew_cap(
            arm, cap_ops.CapConfig(step_deg=90.0, settle_s=0.0, end_gripped=end_gripped))
        assert expected in sentence, sentence
    # And the default sentence matches the module's contract.
    assert ("cap held" if END_GRIPPED else "cap released") in cap_ops.unscrew_cap(
        _arm(), cap_ops.CapConfig(step_deg=90.0, settle_s=0.0))
