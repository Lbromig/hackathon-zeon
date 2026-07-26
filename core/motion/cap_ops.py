"""Cap manipulation: grab, ungrab, and the ratchet unscrew.

The unscrew is a *ratchet* because the tool cabling cannot take a continuous 360°:
the wrist turns 180° with the cap held, opens, turns back 180° with the cap free,
re-grips, and turns again. Two bites therefore back the cap off a full turn while the
wrist itself never travels more than 180° in one go.

    +180  (gripped — cap turns)
     open (let go)
    -180  (free — wrist unwinds, cap stays where it is)
    close (re-grip)
    +180  (gripped — cap turns again)
     open (release the cap)
    -180  (free — wrist returns to where it started)

Net effect per call: the cap rotates ``180 * half_turns`` degrees, the cap is left
released, and the wrist ends exactly where it began. Zero net wrist travel is what makes
the routine repeatable — without the closing unwind each call would walk J6 another 180°
toward its limit. Turns are still pre-flighted against the joint soft limits, because the
*intermediate* angles reach +180 even when the endpoints match.

Rotation is commanded in *joint space* on the tool axis, not as a cartesian yaw. Spinning
the last joint keeps the TCP position fixed by construction; asking IK for a 180° yaw
change invites a different arm configuration and a large unplanned motion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from drivers.capabilities.arm import ArmDriver

# One bite of the ratchet. The cabling limit is the reason this is not 360.
HALF_TURN_DEG = 180.0
DEFAULT_HALF_TURNS = 2          # 2 x 180 = one full turn of the cap
DEFAULT_JOINT_SPEED = 30.0      # deg/s — unscrewing is not a place to hurry
RETURN_TOLERANCE_DEG = 1.0      # how far off "back at the start" is still acceptable


class CapOpError(RuntimeError):
    """A cap operation could not be carried out safely."""


@dataclass
class CapConfig:
    """Bench-specific numbers. Widths are in the gripper's own units (counts)."""
    grip_counts: float | None = None     # None = close fully
    half_turns: int = DEFAULT_HALF_TURNS
    joint_speed: float = DEFAULT_JOINT_SPEED
    settle_s: float = 0.3


def grab_cap(arm: ArmDriver, cfg: CapConfig | None = None) -> str:
    """Close the jaws on the cap."""
    cfg = cfg or CapConfig()
    arm.grip(width=cfg.grip_counts)
    # Build the label separately: a format spec applied to a conditional covers the
    # whole expression, so ':g' would blow up on the "closed" branch.
    width = "closed" if cfg.grip_counts is None else f"{cfg.grip_counts:g}"
    return f"grabbed cap (width={width})"


def ungrab_cap(arm: ArmDriver) -> str:
    """Open the jaws, releasing the cap."""
    arm.release()
    return "released cap"


def plan_unscrew(half_turns: int = DEFAULT_HALF_TURNS) -> list[tuple[str, float]]:
    """The ratchet as a list of (action, degrees) steps — pure, so it is testable.

    Every bite is turn-gripped / open / unwind-free, and the unwind must happen with the
    jaws OPEN or it would simply screw the cap back on. Between bites the jaws re-grip;
    after the last one they do not, so the routine ends with the cap released and the
    wrist back exactly where it started.

    That final unwind is what makes the operation repeatable: net wrist travel is zero,
    so unscrewing twice in a row does not walk J6 toward its limit.
    """
    if half_turns < 1:
        raise CapOpError("half_turns must be >= 1")
    steps: list[tuple[str, float]] = []
    for i in range(half_turns):
        steps.append(("turn", +HALF_TURN_DEG))       # gripped: back the cap off
        steps.append(("open", 0.0))                  # let go before unwinding
        steps.append(("turn", -HALF_TURN_DEG))       # free: wrist returns
        if i < half_turns - 1:
            steps.append(("close", 0.0))             # re-grip for the next bite
    return steps


def unscrew_cap(arm: ArmDriver, cfg: CapConfig | None = None,
                on_step: Callable[[str], None] | None = None) -> str:
    """Run the ratchet. Pre-flights every turn before the first one executes.

    Checking up front matters: aborting halfway leaves the cap partly unscrewed with the
    wrist wound round, which is worse than never having started.
    """
    cfg = cfg or CapConfig()
    steps = plan_unscrew(cfg.half_turns)
    _preflight_turns(arm, steps)

    import time
    index = _tool_axis(arm)
    start = list(arm.get_joints())

    for action, degrees in steps:
        if action == "turn":
            _turn_tool_axis(arm, degrees, cfg.joint_speed)
        elif action == "open":
            arm.release()
        elif action == "close":
            arm.grip(width=cfg.grip_counts)
        time.sleep(cfg.settle_s)
        if on_step:
            on_step(f"{action} {degrees:+.0f}°" if action == "turn" else action)

    turned = HALF_TURN_DEG * cfg.half_turns
    drift = arm.get_joints()[index] - start[index]
    note = "" if abs(drift) <= RETURN_TOLERANCE_DEG else \
        f" WARNING: wrist ended {drift:+.1f}° from start, expected 0"
    return (f"unscrewed {turned:.0f}° in {cfg.half_turns} x {HALF_TURN_DEG:.0f}° bites; "
            f"cap released, wrist returned to start{note}")


def _tool_axis(arm: ArmDriver) -> int:
    """Index of the joint that spins the tool about its own axis (the last one)."""
    return max(0, arm.axis_count - 1)


def _preflight_turns(arm: ArmDriver, steps: list[tuple[str, float]]) -> None:
    """Walk the whole ratchet on paper and check every wrist angle it would visit."""
    index = _tool_axis(arm)
    try:
        angles = list(arm.get_joints())
    except Exception as e:
        raise CapOpError(f"cannot read joint angles to pre-flight the unscrew: {e}") from e

    for action, degrees in steps:
        if action != "turn":
            continue
        angles[index] += degrees
        reason = arm.check_joint_target(angles)
        if reason:
            raise CapOpError(
                f"unscrew refused before starting: J{index + 1} would reach "
                f"{angles[index]:.1f}° — {reason}. Rotate the wrist back before unscrewing."
            )


def _turn_tool_axis(arm: ArmDriver, degrees: float, speed: float) -> None:
    index = _tool_axis(arm)
    deltas = [0.0] * arm.axis_count
    deltas[index] = degrees
    arm.move_joints_relative(deltas, speed=speed, wait=True)
