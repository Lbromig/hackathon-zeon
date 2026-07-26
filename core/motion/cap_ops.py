"""Cap manipulation: grab, ungrab, and the ratchet unscrew.

The unscrew is a *ratchet* because the tool cabling cannot take a continuous 360°:
the wrist turns one **bite** with the cap held, opens, turns back by the same amount
with the cap free, re-grips, and turns again. Several bites therefore back the cap off
a full turn while the wrist itself never travels more than one bite in one go.

    +bite  (gripped — cap turns)
     open  (let go)
    -bite  (free — wrist unwinds, cap stays where it is)
    close  (re-grip)
    +bite  (gripped — cap turns again)
     ...
     open  (release the cap)
    -bite  (free — wrist returns to where it started)

Net effect per call: the cap rotates ``total_deg`` degrees, the cap is left released,
and the wrist ends exactly where it began. Zero net wrist travel is what makes the
routine repeatable — without the closing unwind each call would walk J6 another bite
toward its limit. Turns are still pre-flighted against the joint soft limits, because
the *intermediate* angles reach the full bite even when the endpoints match.

Rotation is commanded in *joint space* on the tool axis, not as a cartesian yaw. Spinning
the last joint keeps the TCP position fixed by construction; asking IK for a 180° yaw
change invites a different arm configuration and a large unplanned motion.

Bite size is a parameter, not a constant (D12/R-ARM-5)
-----------------------------------------------------
The original routine hardcoded 180° bites, which is what the teach tab's ``/cap`` button
still asks for. The engine's ``arm.decap`` action wants **360° in 90° bites**, and that is
the same plan with a different bite: four turn/open/unwind/close cycles instead of two.
Smaller bites make the pre-flight strictly *easier* — peak wrist excursion is one bite, so
halving the bite halves the excursion — which is the reason the decap default is 90 and not
180. Both invariants are properties of the plan shape, not of the number: every bite is
followed by an equal unwind (net zero), and the whole plan is checked before the first move.

``plan_unscrew``/``CapConfig.half_turns`` are kept as the 180° spelling so the teach API
(``backend/app/schemas.py:163``, and the frontend button behind it) keeps working unchanged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from drivers.capabilities.arm import ArmDriver

# One legacy bite of the ratchet. The cabling limit is the reason this is not 360.
HALF_TURN_DEG = 180.0
DEFAULT_HALF_TURNS = 2          # 2 x 180 = one full turn of the cap
#: The decap bite (D12/R-ARM-5). Smaller than `HALF_TURN_DEG` on purpose: peak wrist
#: excursion is one bite, so a 90° bite halves what the pre-flight has to accept.
DEFAULT_STEP_DEG = 90.0
FULL_TURN_DEG = 360.0
DEFAULT_TURNS = 1.0             # one full turn of the cap
DEFAULT_JOINT_SPEED = 30.0      # deg/s — unscrewing is not a place to hurry
RETURN_TOLERANCE_DEG = 1.0      # how far off "back at the start" is still acceptable

#: A step of the plan. ``("turn", deg)`` moves the tool axis; ``("open", 0.0)`` and
#: ``("close", 0.0)`` drive the jaws.
Step = tuple[str, float]


class CapOpError(RuntimeError):
    """A cap operation could not be carried out safely."""


@dataclass
class CapConfig:
    """Bench-specific numbers. Widths are in the gripper's own units (counts).

    Two ways to say how far to unscrew, and they must not both be in force:

    * ``step_deg`` / ``turns`` — the general form. ``turns`` counts **full turns of the
      cap**, ``step_deg`` is one bite. This is what ``arm.decap`` uses (90°, one turn).
    * ``half_turns`` — the legacy 180°-bite spelling the teach API's ``/cap`` button
      speaks. Kept because it is a wire field (``schemas.CapRequest``), not because it is
      the better description.

    Setting either ``step_deg`` or ``turns`` selects the general form and ``half_turns`` is
    then ignored, so a caller cannot accidentally express two different plans at once.
    """
    grip_counts: float | None = None     # None = close fully
    half_turns: int = DEFAULT_HALF_TURNS
    step_deg: float | None = None
    turns: float | None = None
    joint_speed: float = DEFAULT_JOINT_SPEED
    settle_s: float = 0.3
    # Raise the arm by this much (mm, cartesian +Z) at every re-grip, to follow the cap up
    # its own thread. A cap backing off rises as it turns; the jaws are rigidly held by the
    # flange, so without this the gripper keeps re-gripping at the original height and the
    # thread has to force the cap down through the jaws on every bite. Applied while the jaws
    # are OPEN, immediately before closing them, so the lift never drags the cap sideways.
    #
    # The consequence is deliberate and worth stating: the arm does NOT end where it started.
    # It ends `(bites - 1) * lift_per_regrip_mm` higher, which is why the result reports
    # `lifted_mm` separately from the wrist's net travel. Set 0 to disable.
    lift_per_regrip_mm: float = 2.0
    lift_speed: float = 30.0             # mm/s for the lift; unscrewing is not a race
    # Rewind the wrist (jaws open, so the cap does not turn) when it is parked too far round
    # for the plan to fit inside J6's soft limit — see `unwind_tool_axis`.
    #
    # On by default, from the bench: "unscrew refused before starting: J6 would reach 484.3°"
    # is a *starting position* problem, and the wrist being left wound from the previous
    # unscrew is the normal state rather than an error. This does not weaken R-ARM-5's
    # up-front pre-flight: the rewind is jaws-open repositioning that turns no cap, it happens
    # before `preflight_turns`, and a plan that no starting angle could fit still raises out of
    # `required_unwind` before anything moves. What it removes is the refusal for the one case
    # that was only ever about where the wrist happened to be parked.
    #
    # It is still real motion nobody asked for, so it is reported: `unwound_deg` on the result,
    # and the engine's decap handler raises a warning when it is non-zero.
    auto_unwind: bool = True

    @property
    def generalized(self) -> bool:
        """True when this config uses the ``step_deg``/``turns`` form."""
        return self.step_deg is not None or self.turns is not None

    @property
    def bite_deg(self) -> float:
        """One bite of the ratchet, in degrees of tool-axis rotation."""
        if not self.generalized:
            return HALF_TURN_DEG
        return DEFAULT_STEP_DEG if self.step_deg is None else float(self.step_deg)

    @property
    def total_deg(self) -> float:
        """How far the *cap* turns in total."""
        if not self.generalized:
            return HALF_TURN_DEG * self.half_turns
        return FULL_TURN_DEG * (DEFAULT_TURNS if self.turns is None else float(self.turns))

    def plan(self) -> list[Step]:
        """The ratchet this config describes, as steps. Pure."""
        if not self.generalized:
            return plan_unscrew(self.half_turns)
        return plan_ratchet(self.bite_deg, self.total_deg)


@dataclass(frozen=True)
class RatchetResult:
    """What a run of the ratchet actually did.

    ``net_wrist_travel_deg`` is *measured* off the arm afterwards rather than assumed from
    the plan: a ratchet that drifted should show up in the record instead of in the cabling.
    """
    bites: int
    step_deg: float
    total_rotation_deg: float
    net_wrist_travel_deg: float
    preflight_ok: bool
    # Degrees the wrist was rewound (jaws open) before the first bite, to bring the plan
    # inside J6's soft limit. Reported because it is real motion the operator did not ask
    # for: a recovery that happens silently is indistinguishable from a bug.
    unwound_deg: float = 0.0

    @property
    def returned(self) -> bool:
        return abs(self.net_wrist_travel_deg) <= RETURN_TOLERANCE_DEG


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


# --- the plan (pure) ---------------------------------------------------------------

def bite_sizes(step_deg: float = DEFAULT_STEP_DEG,
               total_deg: float = FULL_TURN_DEG) -> list[float]:
    """Split ``total_deg`` of cap rotation into bites of at most ``step_deg``.

    A trailing remainder becomes its own short bite rather than being rounded away or
    rounded up: the caller asked for a specific total rotation, and both silently turning
    the cap further and silently stopping short are wrong in ways nobody would notice.
    """
    if not step_deg > 0:
        raise CapOpError(f"step_deg must be > 0, got {step_deg!r}")
    if not total_deg > 0:
        raise CapOpError(f"total rotation must be > 0, got {total_deg!r}")
    sizes: list[float] = []
    remaining = float(total_deg)
    # 1e-9 rather than 0: 360/90 is exact but 360/110 is not, and a float dust bite of
    # 1e-14 degrees is a commanded move to nowhere.
    while remaining > 1e-9:
        bite = min(float(step_deg), remaining)
        sizes.append(bite)
        remaining -= bite
    return sizes


def plan_ratchet(step_deg: float = DEFAULT_STEP_DEG,
                 total_deg: float = FULL_TURN_DEG) -> list[Step]:
    """The ratchet as a list of (action, degrees) steps — pure, so it is testable.

    Every bite is turn-gripped / open / unwind-free, and the unwind must happen with the
    jaws OPEN or it would simply screw the cap back on. Between bites the jaws re-grip;
    after the last one they do not, so the routine ends with the cap released and the
    wrist back exactly where it started.

    That final unwind is what makes the operation repeatable: net wrist travel is zero,
    so unscrewing twice in a row does not walk J6 toward its limit.
    """
    sizes = bite_sizes(step_deg, total_deg)
    steps: list[Step] = []
    for i, bite in enumerate(sizes):
        steps.append(("turn", +bite))                # gripped: back the cap off
        steps.append(("open", 0.0))                  # let go before unwinding
        steps.append(("turn", -bite))                # free: wrist returns
        if i < len(sizes) - 1:
            steps.append(("close", 0.0))             # re-grip for the next bite
    return steps


def plan_unscrew(half_turns: int = DEFAULT_HALF_TURNS) -> list[Step]:
    """The 180°-bite ratchet — the teach tab's ``/cap`` spelling of `plan_ratchet`."""
    if half_turns < 1:
        raise CapOpError("half_turns must be >= 1")
    return plan_ratchet(HALF_TURN_DEG, HALF_TURN_DEG * half_turns)


def count_bites(steps: list[Step]) -> int:
    """How many bites a plan contains. One ``open`` per bite, by construction."""
    return sum(1 for action, _ in steps if action == "open")


def gripped_rotation(steps: list[Step]) -> float:
    """Total rotation applied to the *cap* — the turns taken with the jaws closed.

    The plan starts gripped, `open` releases and `close` re-grips, so this is the number
    that has to equal the requested total. Useful in tests and in the reported outputs.
    """
    holding, total = True, 0.0
    for action, degrees in steps:
        if action == "open":
            holding = False
        elif action == "close":
            holding = True
        elif holding:
            total += degrees
    return total


def net_wrist_travel(steps: list[Step]) -> float:
    """Where the plan leaves the tool axis relative to its start. Must be 0."""
    return sum(degrees for action, degrees in steps if action == "turn")


# --- execution ---------------------------------------------------------------------

def run_ratchet(arm: ArmDriver, cfg: CapConfig | None = None, *,
                on_step: Callable[[str], None] | None = None,
                on_bite: Callable[[int, int, float], None] | None = None) -> RatchetResult:
    """Run the ratchet and report what it did. Pre-flights every turn before the first one.

    Checking up front matters: aborting halfway leaves the cap partly unscrewed with the
    wrist wound round, which is worse than never having started.

    ``on_bite(index, bites, rotated_deg)`` fires after each completed bite — jaws **open**,
    wrist back at its starting angle, cap loosened by ``rotated_deg`` so far — and before
    the re-grip. That is deliberately the only safe place to pause inside this routine
    (D9), which is why the engine's decap handler puts its ``ctx.checkpoint()`` there: a
    pause anywhere else would hold the run with the wrist wound and the cap half off.

    The jaws are assumed to be **already closed on the cap**; a preceding gripper action
    establishes that. This routine never closes them first, because "close fully" on a cap
    that is not there is a crushed cap, and it cannot tell the difference.
    """
    cfg = cfg or CapConfig()
    steps = cfg.plan()

    # Recovery, not a refusal. The wrist is routinely left wound from a previous unscrew, so
    # "J6 would reach 484°" is a *starting position* problem, not an impossible request: the
    # plan needs a window of travel, and the wrist can be rewound to give it one. Unwinding
    # happens with the jaws OPEN, so the cap does not move — see `unwind_tool_axis`.
    unwound = 0.0
    if cfg.auto_unwind:
        unwound = unwind_tool_axis(arm, required_unwind(arm, steps), cfg)
        if unwound and on_step:
            on_step(f"unwound wrist {-unwound:+.0f}° to make room (jaws open)")
    preflight_turns(arm, steps)

    index = tool_axis(arm)
    start = list(arm.get_joints())
    bites = count_bites(steps)
    bite_no, rotated = 0, 0.0

    for action, degrees in steps:
        if action == "turn":
            turn_tool_axis(arm, degrees, cfg.joint_speed)
        elif action == "open":
            arm.release()
        elif action == "close":
            arm.grip(width=cfg.grip_counts)
        if cfg.settle_s:
            time.sleep(cfg.settle_s)
        if on_step:
            on_step(f"{action} {degrees:+.0f}°" if action == "turn" else action)
        # The unwind is the last motion of a bite: at this point the jaws are open and
        # the wrist is back where the bite started.
        if action == "turn" and degrees < 0:
            bite_no += 1
            rotated += -degrees
            if on_bite:
                on_bite(bite_no, bites, rotated)

    return RatchetResult(
        bites=bites,
        step_deg=cfg.bite_deg,
        total_rotation_deg=gripped_rotation(steps),
        net_wrist_travel_deg=arm.get_joints()[index] - start[index],
        preflight_ok=True,
        unwound_deg=unwound,
    )


def unscrew_cap(arm: ArmDriver, cfg: CapConfig | None = None,
                on_step: Callable[[str], None] | None = None) -> str:
    """`run_ratchet` with a human sentence for the teach tab's action result."""
    cfg = cfg or CapConfig()
    result = run_ratchet(arm, cfg, on_step=on_step)
    note = "" if result.returned else (
        f" WARNING: wrist ended {result.net_wrist_travel_deg:+.1f}° from start, expected 0")
    prefix = ("" if not result.unwound_deg else
              f"rewound wrist {result.unwound_deg:.0f}° first (jaws open, cap untouched); ")
    return (f"{prefix}unscrewed {result.total_rotation_deg:.0f}° in {result.bites} x "
            f"{result.step_deg:.0f}° bites; cap released, wrist returned to start{note}")


def tool_axis(arm: ArmDriver) -> int:
    """Index of the joint that spins the tool about its own axis (the last one)."""
    return max(0, arm.axis_count - 1)


def preflight_turns(arm: ArmDriver, steps: list[Step]) -> None:
    """Walk the whole ratchet on paper and check every wrist angle it would visit.

    Every intermediate angle, not just the endpoints: the plan returns the wrist to where
    it started, so checking only the end would pass a plan whose middle drives J6 past its
    limit. Refusing here rather than at the offending step is the whole point — see
    `run_ratchet`.
    """
    index = tool_axis(arm)
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


def plan_excursion(steps: list[Step]) -> tuple[float, float]:
    """(highest, lowest) tool-axis offset from the start that a plan visits."""
    cur = peak = trough = 0.0
    for action, degrees in steps:
        if action != "turn":
            continue
        cur += degrees
        peak, trough = max(peak, cur), min(trough, cur)
    return peak, trough


def required_unwind(arm: ArmDriver, steps: list[Step]) -> float:
    """How far the tool axis must rotate BACK for every planned turn to become legal.

    0 when the plan already fits. Positive means "rotate the tool axis this many degrees
    negative first". Raises when unwinding cannot help — the plan needs more range than the
    joint has between its limits, so no starting angle would work.
    """
    index = tool_axis(arm)
    limits = arm.limits.joints
    if not limits or index >= len(limits):
        return 0.0                     # no soft limits configured, nothing to solve for
    lo, hi = limits[index]
    start = list(arm.get_joints())[index]
    peak, trough = plan_excursion(steps)

    if (span := peak - trough) > (hi - lo):
        raise CapOpError(
            f"unscrew needs {span:.0f}° of J{index + 1} travel but its soft limit "
            f"[{lo:g}, {hi:g}] only allows {hi - lo:.0f}° — no starting angle can fit this "
            f"plan; reduce the bite or the number of turns"
        )
    unwind = max(0.0, (start + peak) - hi)
    if start - unwind + trough < lo:
        # Unwinding to fit the top would push the bottom of the excursion out instead.
        raise CapOpError(
            f"J{index + 1} cannot be positioned to fit this unscrew: at {start:.1f}° it "
            f"needs to come back {unwind:.0f}° to stay under {hi:g}°, which would take the "
            f"plan's low point past {lo:g}°"
        )
    return unwind


def unwind_tool_axis(arm: ArmDriver, degrees: float, cfg: "CapConfig") -> float:
    """Rotate the tool axis back by ``degrees``, **with the jaws open**.

    The jaws must be open for this: the whole point is to reposition the wrist *without*
    turning the cap, and doing it gripped would screw the cap back down by exactly the
    amount we unwind. Open, rotate, re-grip — the same three motions that end every bite,
    which is what makes this safe to do while holding a cap mid-thread.
    """
    if degrees <= 0:
        return 0.0
    arm.release()
    if cfg.settle_s:
        time.sleep(cfg.settle_s)
    turn_tool_axis(arm, -degrees, cfg.joint_speed)
    arm.grip(width=cfg.grip_counts)
    if cfg.settle_s:
        time.sleep(cfg.settle_s)
    return degrees


def turn_tool_axis(arm: ArmDriver, degrees: float, speed: float) -> None:
    index = tool_axis(arm)
    deltas = [0.0] * arm.axis_count
    deltas[index] = degrees
    arm.move_joints_relative(deltas, speed=speed, wait=True)


# Pre-generalization spellings. Kept as aliases because they were module-private and
# nothing outside imported them; the public names above are what callers should use.
_tool_axis = tool_axis
_preflight_turns = preflight_turns
_turn_tool_axis = turn_tool_axis
