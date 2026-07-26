"""The bench refusal: "J6 would reach 360.0° — J6=360.00° outside soft limit [-360, 360]".

Reported from the real bench on the decap step. Two defects behind one message:

1. `required_unwind` unwound to put the plan's peak *exactly* at the limit — and for a wrist
   sitting exactly at `hi - peak` it computed **zero** unwind, because the peak was not
   *greater* than `hi`. Arithmetically fine; physically useless. A commanded relative move on a
   real arm lands a few hundredths off, so the peak ends up over the limit and the pre-flight
   refuses the plan the unwind existed to make legal. The mock lands exactly, which is why this
   only ever appeared on hardware.
2. The message rounded both numbers, so it claimed 360.00° was outside [-360, 360] — a
   statement that appears to contradict itself, and which sent the operator to rotate a wrist
   that already looked in range.
"""
from __future__ import annotations

import pytest

from core.motion import cap_ops
from drivers.mock import MockArmDriver

FULL_TURN = cap_ops.plan_ratchet(90.0, 360.0)


def _arm(j6: float, *, lo: float = -360.0, hi: float = 360.0) -> MockArmDriver:
    arm = MockArmDriver("right", {"limits": {"joints": [[-360, 360]] * 5 + [[lo, hi]]}})
    arm.connect()
    arm._joints = [0.0] * 5 + [j6]
    return arm


def test_a_four_by_ninety_ratchet_only_peaks_ninety_degrees_from_the_start():
    """Sanity anchor for everything below: the wrist oscillates, it does not accumulate."""
    assert cap_ops.plan_excursion(FULL_TURN) == (90.0, 0.0)


def test_the_wrist_exactly_one_bite_below_the_limit_is_unwound_not_waved_through():
    """The exact bench case. J6 at 270° with a +90° peak and a 360° limit.

    Old behaviour: unwind == 0, because 270 + 90 is not *greater* than 360. Nothing moved, and
    the arm's real angle put the peak over.
    """
    arm = _arm(270.0)
    unwind = cap_ops.required_unwind(arm, FULL_TURN)
    assert unwind >= cap_ops.UNWIND_MARGIN_DEG, (
        "a wrist sitting exactly one bite below the limit must be unwound, not accepted"
    )


def test_the_unwind_leaves_room_for_an_arm_that_does_not_land_exactly():
    """After unwinding, the plan must still pre-flight when the arm lands slightly short.

    This is the whole point of the margin: it is not decoration, it is the difference between
    the fix working on the bench and not.
    """
    for start in (270.0, 270.05, 269.0, 300.0, 359.0):
        arm = _arm(start)
        unwind = cap_ops.required_unwind(arm, FULL_TURN)
        # The arm undershoots the commanded unwind by 0.04°, which is what broke it before.
        arm._joints[5] = start - max(0.0, unwind - 0.04)
        cap_ops.preflight_turns(arm, FULL_TURN)   # must not raise


def test_a_wrist_with_room_to_spare_is_not_moved_at_all():
    """The margin must not make the routine fidget when nothing is wrong."""
    assert cap_ops.required_unwind(_arm(100.0), FULL_TURN) == 0.0


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
    arm = _arm(300.0)
    with pytest.raises(cap_ops.CapOpError) as exc:
        cap_ops.preflight_turns(arm, FULL_TURN)      # 300 + 90 = 390, no unwind applied
    message = str(exc.value)
    assert "past it" in message, f"no excess reported: {message}"
    assert "390.000" in message, f"angle not reported at full precision: {message}"


def test_the_reported_angle_is_never_rounded_into_looking_legal():
    """The specific bench confusion: a hair over the limit must not print as the limit."""
    arm = _arm(270.004)
    with pytest.raises(cap_ops.CapOpError) as exc:
        cap_ops.preflight_turns(arm, FULL_TURN)
    message = str(exc.value)
    assert "360.0°" not in message.replace("360.004", ""), (
        f"the angle rounded to look exactly like the limit: {message}"
    )
    assert "360.004" in message
