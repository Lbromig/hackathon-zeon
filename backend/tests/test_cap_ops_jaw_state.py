"""What the jaws are doing at every step of the ratchet, and during the recovery unwind.

Operator requirement: the jaws must be **closed before the cap is turned**, and when the wrist
is wound too far to start, the recovery must **unwind with the jaws open, then close, then
decap**. Both are physical safety properties rather than implementation details:

* turning with the jaws open turns nothing — the routine reports success having not unscrewed;
* unwinding with the jaws closed screws the cap back down by exactly the amount unwound, so a
  gripped unwind silently undoes the bite that preceded it;
* closing on a cap that is not there, or at full force, crushes it — which is why every close
  in the sequence uses the configured width.

These are asserted as an ordered transcript rather than per-call, because the property is about
the *sequence*: any single call in isolation looks fine.

None of them is a claim about which *way* the wrist turns — that is `cap_ops.UNSCREW_SIGN`,
and it depends on how the gripper is bolted to the flange. A gripped turn goes the loosening
way and an unwind comes back; the transcript is read through `GRIPPED`/`FREE` so the sequence
stays the subject even when the sign flips.
"""
from __future__ import annotations

import pytest

from core.motion import cap_ops
from drivers.mock import MockArmDriver

GRIP = 298.0          # 35 % of the parallel gripper's 0..850, as the workflow commands

SIGN = cap_ops.UNSCREW_SIGN
MARGIN = cap_ops.UNWIND_MARGIN_DEG
#: How a cap-turning and a wrist-returning move appear in the transcript below. The gripped
#: turn takes the loosening sign; every unwind — between bites and in the recovery — opposes it.
GRIPPED = "+" if SIGN > 0 else "-"
FREE = "-" if SIGN > 0 else "+"


def _toward(deg: float) -> float:
    """``deg`` degrees in the loosening direction, signed."""
    return SIGN * deg


class _Recorder(MockArmDriver):
    """A mock arm that records the order of jaw and tool-axis operations."""

    def __init__(self, j6: float = 0.0, *, hi: float = 360.0, lo: float = -360.0) -> None:
        super().__init__("left", {"limits": {"joints": [[-360, 360]] * 5 + [[lo, hi]]}})
        self.connect()
        self._joints = [0.0] * 5 + [j6]
        self.transcript: list[str] = []

    def grip(self, width=None, force=None):
        self.transcript.append(f"close({'full' if width is None else width:g})"
                               if width is not None else "close(full)")
        super().grip(width=width, force=force)

    def release(self):
        self.transcript.append("open")
        super().release()

    def move_joints_relative(self, deltas, speed=None, wait=True):
        self.transcript.append(f"turn({deltas[5]:+.0f})")
        super().move_joints_relative(deltas, speed=speed, wait=wait)


def _cfg(**kw):
    base = dict(grip_counts=GRIP, step_deg=90.0, turns=1.0, settle_s=0.0,
                lift_per_regrip_mm=0.0)
    base.update(kw)
    return cap_ops.CapConfig(**base)


# --- the normal ratchet -------------------------------------------------------

def test_the_cap_is_turned_only_with_the_jaws_closed():
    """Every cap-turning move must be preceded by a close, never by an open."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)                     # the plan closes on the cap before decap runs
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())

    turned = 0
    jaws = "closed"
    for entry in arm.transcript:
        if entry == "open":
            jaws = "open"
        elif entry.startswith("close"):
            jaws = "closed"
        elif entry.startswith(f"turn({GRIPPED}"):
            turned += 1
            assert jaws == "closed", f"the cap was turned with the jaws {jaws}: {arm.transcript}"
    assert turned == 4, f"no cap-turning move was even recognised: {arm.transcript}"


def test_the_unwind_between_bites_happens_with_the_jaws_open():
    """A gripped unwind screws the cap back down, undoing the bite before it."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())

    unwound = 0
    jaws = "closed"
    for entry in arm.transcript:
        if entry == "open":
            jaws = "open"
        elif entry.startswith("close"):
            jaws = "closed"
        elif entry.startswith(f"turn({FREE}"):
            unwound += 1
            assert jaws == "open", f"the wrist unwound with the jaws {jaws}: {arm.transcript}"
    assert unwound == 4, f"no unwind was even recognised: {arm.transcript}"


def test_every_close_in_the_sequence_uses_the_configured_width():
    """Never a full close: that is the crushed-cap path."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())
    assert "close(full)" not in arm.transcript, arm.transcript


def test_the_ratchet_ends_with_the_cap_released():
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())
    last_jaw = next(e for e in reversed(arm.transcript)
                    if e == "open" or e.startswith("close"))
    assert last_jaw == "open", f"the ratchet finished still holding the cap: {arm.transcript}"


# --- the recovery unwind: open, then close, then decap ------------------------

def test_a_wrist_wound_too_far_unwinds_open_then_closes_then_decaps():
    """The operator's requirement, as an ordered transcript.

    A wrist parked 300° round in the loosening direction has less than one 90° bite of room
    before the soft limit it is heading for, so the recovery fires. It must open, unwind,
    close, and only then start turning the cap.

    300 and not 270: a wrist exactly one bite from the limit is deliberately *not*
    repositioned any more — PREFLIGHT_TOLERANCE_DEG absorbs that with the jaws still closed,
    because opening them to buy headroom drops the cap. This test is about a wrist that is
    genuinely wound too far, which is the only case that earns a reposition.
    """
    arm = _Recorder(j6=_toward(300.0))
    arm.grip(width=GRIP)
    arm.transcript.clear()
    result = cap_ops.run_ratchet(arm, _cfg())

    assert abs(result.unwound_deg) >= MARGIN, \
        "the recovery did not fire on a wrist that needed it"

    # The first four operations are the recovery, in this exact order.
    assert arm.transcript[0] == "open", f"recovery did not open first: {arm.transcript[:4]}"
    assert arm.transcript[1].startswith(f"turn({FREE}"), \
        f"recovery did not unwind against the loosening direction: {arm.transcript[:4]}"
    assert arm.transcript[2].startswith("close"), \
        f"recovery did not re-close before decapping: {arm.transcript[:4]}"
    assert arm.transcript[3].startswith(f"turn({GRIPPED}"), \
        f"the first cap turn is not the fourth operation: {arm.transcript[:4]}"


def test_the_recovery_unwind_turns_the_wrist_and_not_the_cap():
    """Open-unwind-close is what makes the reposition free: the cap must not move."""
    arm = _Recorder(j6=_toward(300.0))
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())
    # The recovery's unwind sits strictly between an open and a close.
    i = next(i for i, e in enumerate(arm.transcript) if e.startswith(f"turn({FREE}"))
    assert arm.transcript[i - 1] == "open"
    assert arm.transcript[i + 1].startswith("close")


def test_no_recovery_means_no_spurious_jaw_cycling():
    """A wrist with room must not be opened and closed for nothing — that drops the cap."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    result = cap_ops.run_ratchet(arm, _cfg())
    assert result.unwound_deg == 0.0
    assert arm.transcript[0].startswith(f"turn({GRIPPED}"), (
        f"the sequence opened the jaws before the first turn with no recovery needed: "
        f"{arm.transcript[:3]}"
    )


def test_a_wrist_exactly_one_bite_from_the_limit_keeps_hold_of_the_cap():
    """The second bench report, as a jaw-state property.

    A previous fix unwound to `hi - margin` unconditionally, which made the reposition fire
    for a wrist that was essentially in position — and a reposition opens the jaws, dropping
    the cap. Buying 2° of headroom at the cost of the cap is worse than the refusal it was
    avoiding, so a wrist merely *at* the edge is left alone and pre-flighted as it stands.
    """
    arm = _Recorder(j6=_toward(270.0))            # 90° bite, 360° limit: exactly on the edge
    arm.grip(width=GRIP)
    arm.transcript.clear()
    result = cap_ops.run_ratchet(arm, _cfg())

    assert result.unwound_deg == 0.0
    assert arm.transcript[0].startswith(f"turn({GRIPPED}"), (
        f"the jaws opened before the first bite for a wrist that was in position — that "
        f"drops the cap: {arm.transcript[:3]}"
    )
