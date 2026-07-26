"""The hero workflow: cooperative uncap -> transport -> aspirate, verified.

Written as a generator of WorkflowStepEvent so the API can stream progress
(and retries) to the UI over websocket. Orchestration is the "middle layer":
it maps each step's required capability to concrete drivers, runs the motion,
then calls the matching verification agent and retries on failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from drivers import ArmDriver, DeckLocation, LiquidHandlerDriver, Pose

from core import teach_poses
from core.verification.agents import AGENTS, Evidence

from ..services.device_manager import DeviceManager


class WorkflowError(RuntimeError):
    """A step could not be executed — bad config, missing pose, refused motion.

    Distinct from a *verification* failure: this means the motion never happened,
    so retrying the same step unchanged will fail the same way.
    """


@dataclass
class Step:
    key: str
    capability: str            # capability required (maps to driver method group)
    devices: list[str]         # device ids involved
    verifier: str              # verification agent name


PLAN: list[Step] = [
    Step("uncap",     "dual_arm_manipulation", ["left", "right"], "cap_removed"),
    Step("transport", "arm_transport",         ["right"],         "grasp_secure"),
    Step("present",   "arm_present",           ["right"],         "tube_aligned"),
    Step("aspirate",  "liquid_handling",       ["ot", "right"],   "aspiration_ok"),
]

MAX_ATTEMPTS = 3

# --- choreography -------------------------------------------------------------
# Declared as data rather than buried in if/elif branches, because three people are
# still changing it and it has to stay reviewable in one place. Every `pose` is a
# name taught through the UI (core.teach_poses) — no bench geometry is hardcoded.


@dataclass
class Act:
    """One primitive in a step's choreography."""
    device: str
    kind: str                       # move | grip | release | aspirate
    pose: str = ""                  # taught pose name, for kind="move"
    volume_ul: float = 0.0          # for kind="aspirate"
    deck: str = ""                  # OT deck slot, for kind="aspirate"
    note: str = ""
    # Twin effect (W4): when this act runs, reparent `attach` onto `to` in the digital
    # twin — e.g. a grip attaches the tube to the tool, a release parks the cap. This is
    # what lets grasp_secure / cap_removed turn true on a live run (the tube/cap actually
    # move onto the gripper / into the dropzone in the twin, tracked by arm FK).
    attach: str = ""                # entity id to reparent
    to: str = ""                    # its new parent entity id
    keep_world: bool = True         # preserve world pose across the reparent


# FLOOR-demo choreography (snap-cap: pull the cap straight off, no ratchet loop).
# The dual-arm screw-cap ratchet-unscrew is the TARGET rung and is not encoded here —
# see docs/CAPABILITY_pick_place.md and the fallback ladder in PROJECT_PLAN.md.
#
# Reading of the PLAN: `left` holds the tube body throughout; `right` takes the cap
# off, parks it, then comes back for the tube and presents it to the OT. If the
# intended split is different, this table is the only thing that needs to change.
CHOREOGRAPHY: dict[str, list[Act]] = {
    "uncap": [
        Act("left",  "move",    pose="tube_hold_approach"),
        Act("left",  "move",    pose="tube_hold"),
        Act("left",  "grip",    attach="tube_1", to="left_tool",
            note="clamp the tube body so the cap can be pulled"),
        Act("right", "move",    pose="cap_grasp_approach"),
        Act("right", "move",    pose="cap_grasp"),
        Act("right", "grip",    attach="tube_1_cap", to="right_tool", note="close on the cap"),
        Act("right", "move",    pose="cap_lift", note="snap-cap: straight up, no twist"),
        Act("right", "move",    pose="cap_dropoff"),
        Act("right", "release", attach="tube_1_cap", to="dropzone", note="park the cap"),
        Act("right", "move",    pose="cap_dropoff_retreat"),
    ],
    "transport": [
        Act("right", "move",    pose="tube_grasp_approach"),
        Act("right", "move",    pose="tube_grasp"),
        Act("right", "grip",    attach="tube_1", to="right_tool",
            note="take the open tube from the left arm"),
        Act("left",  "release", note="left lets go once right has it"),
        Act("right", "move",    pose="transport_safe"),
    ],
    "present": [
        Act("right", "move",    pose="present_approach"),
        Act("right", "move",    pose="present_ot", note="tube held under the OT tip"),
    ],
    "aspirate": [
        Act("ot", "aspirate", volume_ul=100.0, deck="A1"),
    ],
}


def run(dm: DeviceManager, *, max_attempts: int = MAX_ATTEMPTS,
        skip_preflight: bool = False) -> Iterator[dict]:
    # Check the whole plan before anything moves. Discovering at step 3 that a pose
    # was never taught leaves an arm holding an open tube in mid-air.
    if not skip_preflight:
        problems = preflight(dm)
        if problems:
            yield {"step": "preflight", "phase": "failed", "attempt": 1,
                   "detail": "; ".join(problems), "problems": problems}
            return
        yield {"step": "preflight", "phase": "passed", "attempt": 1,
               "detail": f"{len(PLAN)} steps, all devices present and poses taught"}

    for step in PLAN:
        attempt = 0
        while True:
            attempt += 1
            yield {"step": step.key, "phase": "started", "attempt": attempt,
                   "devices": step.devices, "capability": step.capability}

            try:
                _execute(step, dm)
            except Exception as e:
                # The motion did not happen, so retrying unchanged would fail the
                # same way — surface it and stop rather than burning attempts.
                yield {"step": step.key, "phase": "failed", "attempt": attempt,
                       "detail": f"{type(e).__name__}: {e}", "error": "execution"}
                return

            yield {"step": step.key, "phase": "verifying", "attempt": attempt}
            result = AGENTS[step.verifier].verify(_collect_evidence(step, dm))

            if result.ok:
                yield {"step": step.key, "phase": "passed", "attempt": attempt,
                       "verification": result.__dict__}
                break
            if attempt >= max_attempts:
                yield {"step": step.key, "phase": "failed", "attempt": attempt,
                       "verification": result.__dict__, "error": "verification"}
                return  # stop the chain; UI surfaces "needs help"
            yield {"step": step.key, "phase": "retrying", "attempt": attempt,
                   "verification": result.__dict__}


def missing_poses(step_key: str) -> dict[str, list[str]]:
    """Taught poses a step needs but that nobody has taught yet, per device.

    Lets the caller pre-flight the whole plan before anything moves — finding out
    at step 3 that a pose is missing leaves an arm holding a tube mid-air.
    """
    wanted: dict[str, list[str]] = {}
    for act in CHOREOGRAPHY.get(step_key, []):
        if act.kind == "move":
            wanted.setdefault(act.device, []).append(act.pose)
    return {dev: miss for dev, names in wanted.items()
            if (miss := teach_poses.require(dev, names))}


def preflight(dm: DeviceManager, plan: list[Step] | None = None) -> list[str]:
    """Everything that would stop the plan running, gathered before it starts."""
    problems: list[str] = []
    for step in (plan or PLAN):
        for dev_id in step.devices:
            try:
                dm.get(dev_id)
            except KeyError:
                problems.append(f"{step.key}: no device {dev_id!r} in the fleet")
        for dev_id, names in missing_poses(step.key).items():
            problems.append(f"{step.key}: {dev_id} has no taught pose(s) {names}")
    return problems


def _execute(step: Step, dm: DeviceManager) -> None:
    """Run a step's choreography against the real drivers.

    Everything goes through the capability interfaces — no vendor SDK above the
    driver layer. A missing taught pose or an unknown act raises: silently doing
    nothing (the previous behaviour) reads to the verifier as "the step ran", which
    is how a workflow reports success while the arm never moved.
    """
    acts = CHOREOGRAPHY.get(step.key)
    if not acts:
        raise WorkflowError(f"step {step.key!r} has no choreography defined")
    for act in acts:
        _run_act(act, dm)


def _run_act(act: Act, dm: DeviceManager) -> None:
    try:
        device = dm.get(act.device)
    except KeyError as e:
        raise WorkflowError(f"no device {act.device!r} in the fleet") from e

    if act.kind == "move":
        arm: ArmDriver = device  # type: ignore[assignment]
        taught = teach_poses.get(act.device, act.pose)
        # Replay joints when we have them: those angles were physically reached, so
        # there is no IK branch to guess at. Falls back to the cartesian pose.
        if taught.joints and len(taught.joints) == arm.axis_count:
            reason = arm.check_joint_target(taught.joints)
            if reason:
                raise WorkflowError(f"{act.device} pose {act.pose!r} rejected: {reason}")
            arm.move_joints(taught.joints, wait=True)
        else:
            pose = Pose(*taught.xyz_rpy)
            reason = arm.check_pose_target(pose)
            if reason:
                raise WorkflowError(f"{act.device} pose {act.pose!r} rejected: {reason}")
            arm.move_to(pose, wait=True)

    elif act.kind == "grip":
        arm = device  # type: ignore[assignment]
        arm.grip()

    elif act.kind == "release":
        arm = device  # type: ignore[assignment]
        arm.release()

    elif act.kind == "aspirate":
        ot: LiquidHandlerDriver = device  # type: ignore[assignment]
        ot.aspirate(volume_ul=act.volume_ul, location=DeckLocation(slot=act.deck))

    else:
        raise WorkflowError(f"unknown act kind {act.kind!r}")

    _apply_twin_effect(act)


def _apply_twin_effect(act: Act) -> None:
    """Reflect a grip/release in the twin by reparenting (W4). Best-effort: guarded by
    entity existence and never allowed to break the physical workflow — the motion is
    what matters, the twin update is bookkeeping that lets verification see the change."""
    if not (act.attach and act.to):
        return
    from ..services import twin

    wm = twin.get_world()
    if wm is None:
        return
    try:
        if act.attach in wm.entities and act.to in wm.entities:
            wm.reparent(act.attach, act.to, keep_world_pose=act.keep_world)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[workflow] twin reparent {act.attach}->{act.to} failed: {e}")


def _collect_evidence(step: Step, dm: DeviceManager) -> Evidence:
    from ..services import twin

    ev = Evidence(world=twin.get_world())
    for cam_id in ("gripper_cam", "overview_cam", "handover_cam"):
        try:
            ev.frames[cam_id] = dm.get(cam_id).capture()  # type: ignore[attr-defined]
        except Exception:
            pass
    for dev_id in step.devices:
        try:
            ev.telemetry[dev_id] = dm.get(dev_id).status()
        except Exception:
            pass
    return ev
