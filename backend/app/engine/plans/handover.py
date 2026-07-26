"""The hero workflow, as data: uncap a tube and present it to the liquid handler.

Twenty steps, exactly as briefed. This module is **data, not behaviour** — it names actions
and waypoints and nothing else. Which driver call each action makes is the handler's
business (``engine/handlers/``), and when it runs is the runner's.

That separation is the point. The previous generation of this file was a hardcoded
``CHOREOGRAPHY`` dict interleaved with a bespoke executor, which is why there were three
overlapping ways to run a sequence and none of them could pause. Here the plan is a list
the engine can index, renumber, pause inside, and inject into.

Two things are deliberately *not* encoded:

* **Speeds.** Every motion action carries a named tier and the tier resolves per device
  against that arm's own soft limits. A raw mm/s here would bypass the clamp.
* **Waypoint geometry.** Names only. The poses live in the taught library and are resolved
  through ``core.waypoints.resolve``, which is also what enforces per-arm ownership — see
  the note on steps 14–18 below.

The step numbers in the comments are the brief's, so this file can be diffed against it.
"""
from __future__ import annotations

from core import waypoints as wp

from ..actions import (Action, ArmDecap, ArmGripper, ArmTraverse, ArmWaypoint,
                       CameraSnapshot, Initialize, LHRelative, Loop,
                       VisionIdentify, VisionSolveOffset, validate_action)


def servo_cameras() -> tuple[str, ...]:
    """Cameras the servo loop looks through — read from config, never named here.

    Two viewpoints, so the offset solve has something to fuse. This is a **function, not a
    constant**, because which physical camera sits at which viewpoint is a bench fact that has
    already changed once: the slot named `gripper_left_cam` is the *right* arm's camera, and
    the slot named `gripper_cam` looks across the room and detected **zero AprilTags in 53
    sampled frames**. A module-level tuple captured at import time invites exactly the drift
    that made a blind camera the servo pair's second view (D19).

    NOTE: on this bench only one unit delivers colour — the others expose their IR/depth UVC
    function (REQUIREMENTS §17.7 C1). The detectors must work on IR frames. Nothing in this
    file assumes colour.
    """
    from core.config import settings

    return tuple(settings.servo_cameras)


#: Convergence gate for the servo loop. `threshold_mm` is the **remaining offset**, not the
#: disagreement between the two views — terminating on disagreement can leave a converged
#: loop running forever, because two views need never agree to within a fixed tolerance
#: (Q4, D16).
OFFSET_THRESHOLD_MM = 1.5
MAX_SERVO_ITERATIONS = 12

#: How far to close on a tube or a cap, as a fraction of the gripper's full opening.
#: Operator-specified: "for the grab tube and cap, the gripper should go to about 35 %".
#:
#: The **fraction is the authoritative number**; the count below is derived from it. The
#: xArm parallel gripper commands in controller counts over a 0..850 range (measured — the
#: taught library records 848-851 when open), so 35 % is ~298 counts.
#:
#: Note the taught poses record 398 counts at `TUBE` and 430 at `CAP_GRAB`, but those are
#: just where the jaws happened to be when the pose was captured — a *recording*, not a
#: target. This is what actually gets commanded.
#:
#: Deliberately NOT clamped by the driver: `ArmDriver.grip` rejects an out-of-range width
#: rather than clamping it, because clamping silently turns a unit-conversion bug into a
#: crushed tube.
GRIP_FRACTION = 0.35
#: Full-scale opening of the parallel gripper, in controller counts. A bench fact about this
#: end-effector. If the gripper is ever changed this is the line to revisit — and the right
#: fix is a `width_from_fraction` on the capability, alongside the existing
#: `width_from_metres`, so a plan need not know counts at all.
GRIPPER_FULL_SCALE_COUNTS = 850.0
GRIP_COUNTS = round(GRIP_FRACTION * GRIPPER_FULL_SCALE_COUNTS)   # 298

# The three TRANSITION_* waypoints and the two LIQUID_HANDLER_* ones were written in the
# brief with a `LEFT_ARM_` prefix, but the arm that visits them is the RIGHT one — it is
# carrying the tube. They are owned by `right`, and `core.waypoints` will refuse to send the
# left arm to them. The prefix was dropped rather than corrected (Q3): waypoints are keyed
# `(device, name)`, so a device name inside the name is redundant and misleads every reader.
# Do not "restore" it.


def _servo_iteration_body() -> list[Action]:
    """One pass of step 19: look, measure, nudge.

    Per camera: take a *fresh* frame, find the tip and the tube in it. Then one solve across
    both views, and one relative move of the liquid handler.

    The frames must be fresh (`fresh=True`): a cached pre-move frame makes the loop measure
    an offset it has already corrected, re-command the same correction, and oscillate. That
    is the failure mode the whole freshness contract exists for (R-CAM-2, D20).

    The body is materialized into the flat plan once per iteration, so iteration 4's frames,
    overlays and solved offset are all inspectable as indexed actions in the workflow tab
    (R-VIS-8) rather than collapsing into one opaque row.
    """
    body: list[Action] = []
    for cam in servo_cameras():
        body += [
            CameraSnapshot(device=cam, fresh=True, store=True, into_slot="frame",
                           label=f"snapshot {cam}"),
            VisionIdentify(device=cam, target="tip", from_slot="frame", into_slot="tip",
                           label=f"identify tip · {cam}"),
            VisionIdentify(device=cam, target="tube", from_slot="frame", into_slot="tube",
                           label=f"identify tube · {cam}"),
        ]
    body.append(
        # device=None means "every servo camera": one solve fusing both views, which is what
        # produces the per-axis uncertainty. `render_overlay` writes the annotated image per
        # view as an artifact on this action (R-VIS-5).
        VisionSolveOffset(device=None, tip_slot="tip", tube_slot="tube",
                          into_slot="selected_offset", render_overlay=True,
                          label="solve offset + overlays"),
    )
    body.append(
        # Reads the solved offset from the blackboard rather than taking literal deltas:
        # the whole point of the loop is that the numbers are computed, not planned.
        # `clamp_mm` bounds one iteration's motion so a bad solve cannot drive the head
        # across the deck (O9).
        LHRelative(device="ot", from_slot="selected_offset", clamp_mm=15.0,
                   speed="slow", label="nudge liquid handler by the solved offset"),
    )
    return body


def build() -> list[Action]:
    """The twenty steps, in order.

    Returns fresh model instances each call so a caller can mutate the plan (inject, expand
    a loop) without editing a module-level singleton.
    """
    right, left = wp.RIGHT, wp.LEFT
    return [
        # 1-2 — both grippers open before anything moves, so neither arm arrives at a tube
        # or a cap already holding something.
        ArmGripper(device=right, state="open", speed="fast", label="right gripper open"),
        ArmGripper(device=left, state="open", speed="fast", label="left gripper open"),

        # 3-5 — right arm goes down onto the tube. Standoff, then lined up, then down slow.
        ArmWaypoint(device=right, waypoint="APPROACH_RACK", speed="fast"),
        ArmWaypoint(device=right, waypoint="APPROACH_TUBE_GRAB", speed="fast"),
        ArmWaypoint(device=right, waypoint="TUBE", speed="slow"),

        # 6 — the tube is now held, at 35 % of full opening.
        ArmGripper(device=right, state="close", width=GRIP_COUNTS,
                   label=f"right gripper close on the tube ({GRIP_FRACTION:.0%})"),

        # 7 — lift it clear of the rack. `medium`: loaded, but nothing is near it.
        ArmWaypoint(device=right, waypoint="APPROACH_TUBE_TRANSFER", speed="medium"),

        # 8-10 — left arm takes the cap, while the right arm holds the tube steady.
        ArmWaypoint(device=left, waypoint="APPROACH_CAP_GRAB", speed="fast"),
        ArmWaypoint(device=left, waypoint="CAP_GRAB", speed="slow"),
        ArmGripper(device=left, state="close", width=GRIP_COUNTS,
                   label=f"left gripper close on the cap ({GRIP_FRACTION:.0%})"),

        # 11 — decap: 360° in 90° bites, rewinding the wrist between each so net wrist
        # travel is zero. Without the rewind, every run walks the tool joint another 360°
        # toward its limit; with it, the operation is repeatable (R-ARM-5, D12).
        # `grip_counts` is the width it re-grips to between bites — the same 35 %, so the
        # ratchet does not re-close harder or looser than the initial grab.
        ArmDecap(device=left, step_deg=90.0, turns=1.0, speed="slow",
                 grip_counts=GRIP_COUNTS, label="unscrew cap · 4 × 90°"),

        # 12-13 — cap up and away. SLOW then FAST, not the other way round: the slow move is
        # the one lifting the loosened cap clear of the tube mouth.
        ArmWaypoint(device=left, waypoint="APPROACH_CAP_STORE", speed="slow"),
        ArmWaypoint(device=left, waypoint="CAP_STORE", speed="fast"),
        ArmGripper(device=left, state="open", label="release the cap into its store"),

        # 14-16 — the long carry, as ONE traverse action rather than three moves, so the
        # controller can blend through the waypoints instead of stopping at each. Owned by
        # `right` despite the brief's LEFT_ARM_ prefix — see the note above.
        ArmTraverse(
            device=right,
            waypoints=["TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE",
                       "TRANSITION_LIQUID_HANDLER_TABLE"],
            blend_deg=5.0, speed="fast", label="traverse to the liquid-handler table",
        ),

        # 17-18 — onto the deck. Standoff outside the envelope, then the slow precision move
        # the servo loop starts from.
        ArmWaypoint(device=right, waypoint="LIQUID_HANDLER_APPROACH_DECK", speed="fast"),
        ArmWaypoint(device=right, waypoint="LIQUID_HANDLER_DECK", speed="slow"),

        # 19 — close the loop on the tip-to-tube offset. Terminates on the remaining offset
        # falling below threshold; `no_progress_abort` catches a loop that is running but not
        # improving (a stale or wrong-signed calibration), which is a different failure from
        # one that simply needs more iterations.
        Loop(
            body=_servo_iteration_body(),
            until="offset_within_threshold",
            threshold_mm=OFFSET_THRESHOLD_MM,
            watch_slot="selected_offset",
            max_iterations=MAX_SERVO_ITERATIONS,
            no_progress_abort=3,
            label="servo the liquid handler onto the tube",
        ),

        # 20 — retract. Positive dz is up; the tube stays in the right arm's jaws.
        LHRelative(device="ot", dz=40.0, speed="medium", clamp_mm=60.0,
                   label="liquid handler retracts Z"),
    ]


def build_startup(home_after: bool = True) -> list[Action]:
    """The initialization plan: find, connect and prepare every device, then home the arms.

    A plan rather than an imperative boot function, so initialization gets the same indices,
    states, per-action logs and pause behaviour as everything else — and so "reinitialize
    device X" is literally this code with a device set (D5, R-INIT-5/7).

    Homing is part of it, and an arm with no taught HOME must produce a *warning* and be
    skipped rather than being sent to a guessed pose (R-INIT-4).
    """
    return [Initialize(device=None, home_after=home_after, speed="slow",
                       label="initialize all devices")]


def waypoints_used() -> list[tuple[str, str]]:
    """Every `(device, waypoint)` pair the workflow visits, in first-visit order.

    Used to pre-flight the whole plan before the first motion, and to drive the teaching
    checklist. Derived from the plan rather than hand-listed, so a waypoint added to the
    workflow cannot be forgotten in the readiness check.
    """
    seen: list[tuple[str, str]] = []

    def note(device: str | None, name: str) -> None:
        pair = (device or "", name)
        if pair not in seen:
            seen.append(pair)

    def walk(actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, ArmWaypoint):
                note(a.device, a.waypoint)
            elif isinstance(a, ArmTraverse):
                for name in a.waypoints:
                    note(a.device, name)
            elif isinstance(a, Loop):
                walk(list(a.body))

    walk(build())
    return seen


def validate() -> list[Action]:
    """Round-trip the plan through the action union, so a bad field fails at import time.

    Cheap insurance: this module is hand-written data, and a typo in a field name would
    otherwise surface as a handler receiving a default it never expected.
    """
    return [validate_action(a.model_dump()) for a in build()]


__all__ = ["MAX_SERVO_ITERATIONS", "OFFSET_THRESHOLD_MM", "servo_cameras", "build",
           "build_startup", "validate", "waypoints_used"]
