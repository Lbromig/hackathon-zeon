"""User-built action sequences: an ordered walkthrough of waypoints and actions.

This is the editable sibling of the hard-coded ``CHOREOGRAPHY`` in the hero workflow.
That one is fixed in code because it is the demo; this one is built in the UI, so an
operator can compose "tube rack -> Opentrons deck" from the poses they just taught,
step through it, and change it without touching Python.

Four actions, matching what the arm can actually do:

    move     drive to a taught waypoint (joint replay — the configuration the arm
             physically reached, so there is no IK branch to guess at)
    grip     close the jaws, optionally to a width
    ungrip   open the jaws
    unscrew  the cap ratchet (see core/motion/cap_ops.py)

Two rules run through the whole module:

* **Pre-flight the entire sequence before the first move.** Discovering at step 7 that a
  pose was never taught leaves the arm holding a tube in mid-air. Every check that can be
  made statically is made up front.
* **A step that cannot run must say so, not be skipped.** A silently-skipped step reads
  downstream as "the sequence ran", which is how a robot reports success having done
  nothing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from drivers.capabilities.arm import ArmDriver

from . import teach_poses
from .config import settings
from .motion import cap_ops

ACTIONS = ("move", "grip", "ungrip", "unscrew")
_ENV_VAR = "HZ_SEQUENCES_FILE"


class SequenceError(ValueError):
    """A sequence cannot be built or run."""


@dataclass
class Step:
    action: str
    device: str
    pose: str = ""                    # move: taught pose name
    width: float | None = None        # grip: gripper units; None = close fully
    half_turns: int = 2               # unscrew: 180 deg bites
    speed: float | None = None
    note: str = ""

    def describe(self) -> str:
        if self.action == "move":
            return f"{self.device}: move to {self.pose!r}"
        if self.action == "grip":
            return f"{self.device}: grip" + (f" to {self.width:g}" if self.width is not None else "")
        if self.action == "ungrip":
            return f"{self.device}: ungrip"
        if self.action == "unscrew":
            return f"{self.device}: unscrew {self.half_turns}x180°"
        return f"{self.device}: {self.action}"


@dataclass
class Sequence:
    name: str
    steps: list[Step] = field(default_factory=list)
    note: str = ""
    updated_at: str = ""


# --- validation ---------------------------------------------------------------

def validate_step(step: Step) -> None:
    if step.action not in ACTIONS:
        raise SequenceError(f"unknown action {step.action!r}; expected one of {list(ACTIONS)}")
    if not step.device:
        raise SequenceError(f"step {step.action!r} has no device")
    if step.action == "move" and not step.pose:
        raise SequenceError("a 'move' step needs a taught pose name")
    if step.action == "unscrew" and step.half_turns < 1:
        raise SequenceError("unscrew needs at least one 180° bite")


def preflight(seq: Sequence, arms: dict[str, ArmDriver]) -> list[str]:
    """Everything that would stop the sequence running, gathered before it starts.

    Static checks only — pose exists, device exists, joint target is inside the soft
    limits. It cannot know whether the tube is actually in the rack; that is what the
    verification agents are for.
    """
    problems: list[str] = []
    for i, step in enumerate(seq.steps, start=1):
        label = f"step {i} ({step.describe()})"
        try:
            validate_step(step)
        except SequenceError as e:
            problems.append(f"{label}: {e}")
            continue

        arm = arms.get(step.device)
        if arm is None:
            problems.append(f"{label}: no device {step.device!r} in the fleet")
            continue

        if step.action == "move":
            try:
                taught = teach_poses.get(step.device, step.pose)
            except teach_poses.MissingPose as e:
                problems.append(f"{label}: {e}")
                continue
            if taught.joints and len(taught.joints) == arm.axis_count:
                reason = arm.check_joint_target(taught.joints)
                if reason:
                    problems.append(f"{label}: {reason}")
        elif step.action in ("grip", "ungrip") and not arm.gripper_info.supports_width \
                and step.width is not None:
            problems.append(
                f"{label}: the {arm.gripper_info.kind} gripper has no commandable width"
            )
    return problems


# --- execution ----------------------------------------------------------------

def run_step(step: Step, arm: ArmDriver) -> str:
    """Execute one step. Raises rather than skipping — see the module docstring."""
    validate_step(step)

    if step.action == "move":
        taught = teach_poses.get(step.device, step.pose)
        # Prefer the recorded joints: the arm physically reached them, so replay cannot
        # pick a different IK branch and swing somewhere unexpected.
        if taught.joints and len(taught.joints) == arm.axis_count:
            reason = arm.check_joint_target(taught.joints)
            if reason:
                raise SequenceError(f"{step.pose!r} rejected: {reason}")
            arm.move_joints(taught.joints, speed=step.speed, wait=True)
            return f"moved to {step.pose!r} (joint replay)"
        from drivers.capabilities.arm import Pose
        pose = Pose(*taught.xyz_rpy)
        reason = arm.check_pose_target(pose)
        if reason:
            raise SequenceError(f"{step.pose!r} rejected: {reason}")
        arm.move_to(pose, speed=step.speed, wait=True)
        return f"moved to {step.pose!r} (cartesian)"

    if step.action == "grip":
        arm.grip(width=step.width)
        return "gripped" + (f" to {step.width:g}" if step.width is not None else "")

    if step.action == "ungrip":
        arm.release()
        return "released"

    cfg = cap_ops.CapConfig(grip_counts=step.width, half_turns=step.half_turns,
                            joint_speed=step.speed or cap_ops.DEFAULT_JOINT_SPEED)
    return cap_ops.unscrew_cap(arm, cfg)


def run(seq: Sequence, arms: dict[str, ArmDriver], *,
        skip_preflight: bool = False,
        on_step: Callable[[int, str], None] | None = None) -> Iterator[dict[str, Any]]:
    """Run every step, yielding one event each. Stops at the first failure.

    A generator so the caller can stream progress; the walkthrough UI runs one step at a
    time via ``run_step`` instead, which is the same code path.
    """
    if not skip_preflight:
        problems = preflight(seq, arms)
        if problems:
            yield {"index": 0, "phase": "preflight", "ok": False,
                   "detail": "; ".join(problems), "problems": problems}
            return
        yield {"index": 0, "phase": "preflight", "ok": True,
               "detail": f"{len(seq.steps)} steps ready"}

    for i, step in enumerate(seq.steps, start=1):
        yield {"index": i, "phase": "started", "ok": True, "detail": step.describe()}
        try:
            detail = run_step(step, arms[step.device])
        except Exception as e:
            yield {"index": i, "phase": "failed", "ok": False,
                   "detail": f"{type(e).__name__}: {e}"}
            return
        if on_step:
            on_step(i, detail)
        yield {"index": i, "phase": "done", "ok": True, "detail": detail}

    yield {"index": len(seq.steps), "phase": "complete", "ok": True,
           "detail": f"ran {len(seq.steps)} steps"}


# --- persistence --------------------------------------------------------------

def sequences_file() -> str:
    override = os.getenv(_ENV_VAR)
    if override:
        return override
    return os.path.join(os.path.dirname(settings.teach_poses_file), "sequences.json")


def load() -> dict[str, Any]:
    p = sequences_file()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[sequences] could not read {p}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def get(name: str) -> Sequence:
    entry = load().get(name)
    if entry is None:
        raise LookupError(f"no sequence {name!r} (known: {sorted(load()) or 'none'})")
    return Sequence(
        name=name,
        steps=[Step(**s) for s in entry.get("steps", [])],
        note=entry.get("note", ""),
        updated_at=entry.get("updated_at", ""),
    )


def save(seq: Sequence) -> None:
    for step in seq.steps:
        validate_step(step)
    store = load()
    store[seq.name] = {
        "name": seq.name,
        "steps": [vars(s) for s in seq.steps],
        "note": seq.note,
        "updated_at": seq.updated_at,
    }
    _write(store)


def delete(name: str) -> bool:
    store = load()
    if store.pop(name, None) is None:
        return False
    _write(store)
    return True


def _write(store: dict[str, Any]) -> None:
    p = sequences_file()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, p)      # atomic: never leave a half-written sequence
