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
"""
from __future__ import annotations

import pytest

from core.motion import cap_ops
from drivers.mock import MockArmDriver

GRIP = 298.0          # 35 % of the parallel gripper's 0..850, as the workflow commands


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
    """Every gripped (positive) turn must be preceded by a close, never by an open."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)                     # the plan closes on the cap before decap runs
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())

    jaws = "closed"
    for entry in arm.transcript:
        if entry == "open":
            jaws = "open"
        elif entry.startswith("close"):
            jaws = "closed"
        elif entry.startswith("turn(+"):
            assert jaws == "closed", f"the cap was turned with the jaws {jaws}: {arm.transcript}"


def test_the_unwind_between_bites_happens_with_the_jaws_open():
    """A gripped unwind screws the cap back down, undoing the bite before it."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())

    jaws = "closed"
    for entry in arm.transcript:
        if entry == "open":
            jaws = "open"
        elif entry.startswith("close"):
            jaws = "closed"
        elif entry.startswith("turn(-"):
            assert jaws == "open", f"the wrist unwound with the jaws {jaws}: {arm.transcript}"


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

    J6 at 270° with a +90° peak leaves no room under a 360° limit, so the recovery fires. It
    must open, unwind, close, and only then start turning the cap.
    """
    arm = _Recorder(j6=270.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    result = cap_ops.run_ratchet(arm, _cfg())

    assert result.unwound_deg > 0, "the recovery did not fire on a wrist that needed it"

    # The first four operations are the recovery, in this exact order.
    assert arm.transcript[0] == "open", f"recovery did not open first: {arm.transcript[:4]}"
    assert arm.transcript[1].startswith("turn(-"), \
        f"recovery did not unwind negative: {arm.transcript[:4]}"
    assert arm.transcript[2].startswith("close"), \
        f"recovery did not re-close before decapping: {arm.transcript[:4]}"
    assert arm.transcript[3].startswith("turn(+"), \
        f"the first cap turn is not the fourth operation: {arm.transcript[:4]}"


def test_the_recovery_unwind_turns_the_wrist_and_not_the_cap():
    """Open-unwind-close is what makes the reposition free: the cap must not move."""
    arm = _Recorder(j6=270.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    cap_ops.run_ratchet(arm, _cfg())
    # The recovery's negative turn sits strictly between an open and a close.
    i = next(i for i, e in enumerate(arm.transcript) if e.startswith("turn(-"))
    assert arm.transcript[i - 1] == "open"
    assert arm.transcript[i + 1].startswith("close")


def test_no_recovery_means_no_spurious_jaw_cycling():
    """A wrist with room must not be opened and closed for nothing — that drops the cap."""
    arm = _Recorder(j6=0.0)
    arm.grip(width=GRIP)
    arm.transcript.clear()
    result = cap_ops.run_ratchet(arm, _cfg())
    assert result.unwound_deg == 0.0
    assert arm.transcript[0].startswith("turn(+"), (
        f"the sequence opened the jaws before the first turn with no recovery needed: "
        f"{arm.transcript[:3]}"
    )
