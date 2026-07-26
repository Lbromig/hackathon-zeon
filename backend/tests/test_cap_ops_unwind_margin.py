"""The two bench refusals about *where the wrist is parked*, and the split that resolved them.

The first was "J6 would reach 360.0° — J6=360.00° outside soft limit [-360, 360]": a message
that appears to contradict itself, because both numbers were rounded and the wrist was a few
hundredths past the limit. Two things were wrong at once, and the fix for each is a different
constant — the split between them is the whole subject of this file.

* :data:`cap_ops.PREFLIGHT_TOLERANCE_DEG` absorbs position dust with the jaws **closed**. A
  wrist sitting *at* the limit, or 0.004° past it because that is how an arm reports 360°, is
  not a wrist wound too far. It is pre-flighted as it stands and the cap is never let go.
* :data:`cap_ops.UNWIND_MARGIN_DEG` applies **only** once a reposition is unavoidable. Then it
  is free, and it is necessary: unwinding to put the plan's extreme *exactly* on the limit is
  arithmetically sufficient and physically useless, because a commanded relative move lands a
  few hundredths off and the pre-flight then refuses the very plan the unwind existed to make
  legal. The mock lands exactly, which is why that only ever appeared on hardware.

The order matters and is the second bench report: an earlier fix took the margin
unconditionally, which fired the reposition for a wrist that was essentially in position — and
a reposition **opens the jaws**, which drops the cap. Buying 2° of headroom at the cost of the
cap is worse than the refusal it was avoiding.

Which limit any of this is about depends on `cap_ops.UNSCREW_SIGN`: a ratchet runs one bite
away from its start in the loosening direction, so with the bench's negative sign it is the
**lower** limit that binds and the upper one that is irrelevant. Nothing here names a limit
directly; `_toward` mirrors every angle with the sign.
"""
from __future__ import annotations

import math

import pytest

from core.motion import cap_ops
from drivers.mock import MockArmDriver

FULL_TURN = cap_ops.plan_ratchet(90.0, 360.0)
BITE = 90.0

SIGN = cap_ops.UNSCREW_SIGN
MARGIN = cap_ops.UNWIND_MARGIN_DEG
TOL = cap_ops.PREFLIGHT_TOLERANCE_DEG


def _toward(deg: float) -> float:
    """``deg`` degrees in the loosening direction, signed."""
    return SIGN * deg


def _arm(j6: float, *, lo: float = -360.0, hi: float = 360.0) -> MockArmDriver:
    arm = MockArmDriver("right", {"limits": {"joints": [[-360, 360]] * 5 + [[lo, hi]]}})
    arm.connect()
    arm._joints = [0.0] * 5 + [j6]
    return arm


def test_a_four_by_ninety_ratchet_only_peaks_ninety_degrees_from_the_start():
    """Sanity anchor for everything below: the wrist oscillates, it does not accumulate.

    And it oscillates in one direction only — one end of the excursion is exactly 0 — which is
    what decides which soft limit the plan can ever reach.
    """
    peak, trough = cap_ops.plan_excursion(FULL_TURN)
    assert (peak, trough) == (max(0.0, _toward(BITE)), min(0.0, _toward(BITE)))


def test_the_wrist_exactly_one_bite_from_the_limit_keeps_its_grip():
    """The bench's second report. Not repositioned — repositioning would drop the cap.

    A wrist at exactly `limit - bite` fits, and "fits" is not improved by opening the jaws to
    make it fit with room to spare. This is the case the first attempt at the margin got wrong
    by unwinding unconditionally.
    """
    arm = _arm(_toward(270.0))
    assert cap_ops.required_unwind(arm, FULL_TURN) == 0.0
    cap_ops.preflight_turns(arm, FULL_TURN)       # accepted, jaws still closed


def test_position_dust_up_to_the_tolerance_is_absorbed_rather_than_repositioned():
    """The bench's first report: an arm reports 360° as 360.004°, and that must not be read as
    a wrist wound past its limit. The whole excess is inside PREFLIGHT_TOLERANCE_DEG."""
    for dust in (0.004, TOL / 2, TOL):
        arm = _arm(_toward(270.0 + dust))
        assert cap_ops.required_unwind(arm, FULL_TURN) == 0.0, dust
        cap_ops.preflight_turns(arm, FULL_TURN)   # must not raise


def test_a_wrist_genuinely_wound_too_far_is_repositioned_with_margin():
    """Past the tolerance the jaws have to open regardless, so the margin is then free — and
    it is what makes the reposition survive an arm that does not land exactly."""
    arm = _arm(_toward(270.0 + TOL + 0.5))
    unwind = cap_ops.required_unwind(arm, FULL_TURN)
    assert abs(unwind) >= MARGIN, (
        "a wrist genuinely past the limit must be repositioned, not waved through")
    assert unwind == pytest.approx(_toward(TOL + 0.5 + MARGIN))


def test_the_unwind_leaves_room_for_an_arm_that_does_not_land_exactly():
    """After unwinding, the plan must still pre-flight when the arm lands slightly short.

    This is the whole point of the margin: it is not decoration, it is the difference between
    the fix working on the bench and not.
    """
    for start_deg in (270.0, 270.05, 269.0, 300.0, 359.0):
        start = _toward(start_deg)
        arm = _arm(start)
        unwind = cap_ops.required_unwind(arm, FULL_TURN)
        # `unwind_tool_axis` commands `-unwind`; the arm undershoots it by 0.04°, which is
        # what broke this on hardware.
        applied = -unwind
        if applied:
            applied -= math.copysign(0.04, applied)
        arm._joints[5] = start + applied
        cap_ops.preflight_turns(arm, FULL_TURN)   # must not raise


def test_a_wrist_with_room_to_spare_is_not_moved_at_all():
    """The margin must not make the routine fidget when nothing is wrong."""
    assert cap_ops.required_unwind(_arm(_toward(100.0)), FULL_TURN) == 0.0


@pytest.mark.parametrize("sign", (-1.0, +1.0))
def test_both_soft_limits_are_guarded_whichever_way_the_cap_loosens(sign):
    """Guarding only the top — as this did while the gripped turn was hardcoded positive —
    silently stops guarding anything the moment the loosening direction flips."""
    plan = cap_ops.plan_ratchet(BITE, 360.0, sign)
    start = sign * (270.0 + TOL + 1.0)
    arm = _arm(start)

    unwind = cap_ops.required_unwind(arm, plan)
    assert abs(unwind) >= MARGIN, "the limit the plan runs toward was not guarded"

    arm._joints[5] = start - unwind               # what `unwind_tool_axis` commands
    cap_ops.preflight_turns(arm, plan)            # and then the plan fits


def test_a_plan_that_no_starting_angle_can_fit_still_raises_before_anything_moves():
    """The margin must not turn an impossible plan into a different, later failure."""
    arm = _arm(0.0, lo=-45.0, hi=45.0)          # 90° of range, a 90° peak, plus margins
    with pytest.raises(cap_ops.CapOpError, match="no starting angle can fit"):
        cap_ops.required_unwind(arm, FULL_TURN)


def test_the_infeasible_message_states_the_usable_range_not_just_the_limit():
    """An operator needs to know the margin is included, or the arithmetic looks wrong."""
    arm = _arm(0.0, lo=-45.0, hi=45.0)
    with pytest.raises(cap_ops.CapOpError) as exc:
        cap_ops.required_unwind(arm, FULL_TURN)
    assert "margin" in str(exc.value)


def test_a_refusal_reports_how_far_past_the_limit_it_would_go():
    """The message must never appear to contradict itself.

    A wrist parked well past the limit cannot be helped by unwinding within margin, so
    `preflight_turns` refuses — and must say by how much, at a precision that makes the number
    believable rather than looking equal to the limit.
    """
    arm = _arm(_toward(300.0))
    with pytest.raises(cap_ops.CapOpError) as exc:
        cap_ops.preflight_turns(arm, FULL_TURN)   # 300 + 90 = 390, no unwind applied
    message = str(exc.value)
    assert "past it" in message, f"no excess reported: {message}"
    assert "390.000" in message, f"angle not reported at full precision: {message}"
    assert "30.000° past it" in message, f"excess not reported exactly: {message}"


def test_the_reported_angle_is_never_rounded_into_looking_legal():
    """The specific bench confusion: a hair over the limit must not print as the limit.

    A hair *within* the tolerance is now accepted outright — that is the other half of the fix
    — so the smallest refusable overshoot is just past PREFLIGHT_TOLERANCE_DEG. It still has to
    print as itself rather than as the limit it exceeds, which is what read as a contradiction.
    """
    over = TOL + 0.1
    arm = _arm(_toward(270.0 + over))
    with pytest.raises(cap_ops.CapOpError) as exc:
        cap_ops.preflight_turns(arm, FULL_TURN)
    message = str(exc.value)
    assert f"{360.0 + over:.3f}" in message, (
        f"the angle rounded to look exactly like the limit: {message}")
    assert "past it" in message, message
    assert "360.000°" not in message, message
