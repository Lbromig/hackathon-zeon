"""The hero workflow: cooperative uncap -> transport -> aspirate, verified.

Written as a generator of WorkflowStepEvent so the API can stream progress
(and retries) to the UI over websocket. Orchestration is the "middle layer":
it maps each step's required capability to concrete drivers, runs the motion,
then calls the matching verification agent and retries on failure.

Phases yielded per step: started, verifying, then one of passed, retrying,
failed, escalated.

failed and escalated are both terminal, and they do not mean the same thing.
Treating them as one outcome is what this module used to get wrong:

* failed:    the motion ran up to max_attempts and verification never passed.
             Retrying was the right thing to try, because the assumption behind
             a retry is that the action simply did not take effect.
* escalated: two independent sensor channels contradict each other, so the
             physical state is not known. There is nothing to retry, because a
             retry assumes we know what state we are starting from. If depth
             says the cap is still on while torque says it came off, then either
             the cap has snapped or the tube is being crushed, and repeating the
             unscrew makes whichever it is worse. Stop on the first one and put
             it in front of a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from drivers import ArmDriver, DeckLocation, LiquidHandlerDriver, Pose

from core.config import GRASP_WIDTH_M, HOLDING_ARM, TURNING_ARM
from core.verification.agents import AGENTS, Evidence

from ..services.device_manager import DeviceManager

# Cameras to grab evidence frames from. These must track core.config's fleet ids:
# the previous list ("on_arm", "external") predated the three-camera rig, so every
# lookup missed and the verifiers silently ran with no vision channel at all.
EVIDENCE_CAMERAS = ("gripper_cam", "overview_cam", "handover_cam")

# The during-trace hook handed to the motion code. Calling it records one
# telemetry snapshot, taken at that instant, into the step's during list.
Sampler = Callable[[], None]

# What run() calls to perform one step. Injectable so the orchestration logic
# (retry, escalate, sampling) can be driven with no drivers attached.
Executor = Callable[["Step", "DeviceManager", Sampler], None]


def _no_sample() -> None:
    """Default hook: record nothing.

    Present so ``_execute`` keeps its two-argument call shape for callers that
    are not collecting a trace (backend.app.agent.tools.Skill.run is one).
    """


@dataclass
class Step:
    key: str
    capability: str            # capability required (maps to driver method group)
    devices: list[str]         # device ids involved
    verifier: str              # verification agent name
    params: dict[str, Any] = field(default_factory=dict)  # targets the verifier checks


def build_plan() -> list[Step]:
    """Steps for one run. Built per call so an arm-role change takes effect
    without reimporting the module."""
    return [
        # These params leave CapRemovedAgent's depth channel inactive, and it stays
        # that way until someone goes to the bench. Two separate gaps, neither of
        # which can be closed by picking a plausible number here:
        #  1. no depth_scale / cap_roi / cap_reference. The reference is a
        #     HeightStat measured over the tube mouth *while capped*, and the scale
        #     is read off the device (the D405 is 1e-4 m per count where the rest
        #     of the D400 series is 1e-3, so a guess is wrong by 10x and still
        #     looks plausible). See core/verification/depth_height.py.
        #  2. EVIDENCE_CAMERAS carries no depth device, so evidence.frames never
        #     has the "depth" key the agent reads, and core.config.settings.fleet
        #     has no depth camera to add to it.
        # Until both are fixed, cap_removed here has at most torque and vision.
        # Not "torque alone": cv2 is a declared dependency (opencv-contrib-python
        # in pyproject.toml), so on a fitted bench the cap fiducial channel is
        # live, the two can fuse above SINGLE_CHANNEL_CAP, and they can also
        # contradict each other. What is lost is the only channel that measures
        # the tube rather than a proxy for it, and so the only channel that could
        # catch the two proxies being wrong together. None of this has been
        # checked on the bench.
        Step("uncap", "dual_arm_manipulation", [HOLDING_ARM, TURNING_ARM], "cap_removed",
             {"turning_arm": TURNING_ARM, "holding_arm": HOLDING_ARM,
              "overview_camera": "overview_cam"}),
        Step("transport", "arm_transport", [HOLDING_ARM], "grasp_secure",
             {"holding_arm": HOLDING_ARM, "grasp_width_m": GRASP_WIDTH_M}),
        Step("present", "arm_present", [HOLDING_ARM], "tube_aligned",
             {"holding_arm": HOLDING_ARM}),
        Step("aspirate", "liquid_handling", ["ot", HOLDING_ARM], "aspiration_ok",
             {"liquid_handler": "ot"}),
    ]


PLAN: list[Step] = build_plan()

MAX_ATTEMPTS = 3


def run(dm: DeviceManager, *, max_attempts: int = MAX_ATTEMPTS,
        plan: list[Step] | None = None,
        execute: Executor | None = None) -> Iterator[dict]:
    """Run the plan, yielding one event per phase transition.

    ``execute`` defaults to the real capability -> driver mapping. It is
    injectable so the orchestration itself (retry budget, escalation, the during
    trace) can be exercised with no drivers, no threads and no clock.
    """
    run_step: Executor = execute if execute is not None else _execute
    for step in (plan if plan is not None else build_plan()):
        attempt = 0
        while True:
            attempt += 1
            yield {"step": step.key, "phase": "started", "attempt": attempt,
                   "devices": step.devices, "capability": step.capability}

            # Snapshot *before* the motion: the verifiers measure change (a torque
            # collapse, a marker that moved), which is unmeasurable from the
            # after-state alone.
            before = _telemetry(step, dm)
            before_frames = _frames(dm)

            # And sample *while* the motion runs. CapRemovedAgent finds the
            # unscrewing torque peak in Evidence.during, and a peak only exists
            # under load: by the time the step returns the wrist has unloaded
            # again, which is the whole reason the pass condition is a drop. This
            # list was never populated, so `during` was always empty, `peak` fell
            # back to the pre-step (unloaded) reading, and the torque channel
            # reported "wrist never loaded" on every run whatever the arm did.
            during: list[dict[str, Any]] = []
            run_step(step, dm, _sampler(step, dm, during))

            yield {"step": step.key, "phase": "verifying", "attempt": attempt}
            evidence = Evidence(
                frames=_frames(dm),
                telemetry=_telemetry(step, dm),
                before=before,
                before_frames=before_frames,
                during=during,
                expected=dict(step.params),
            )
            result = AGENTS[step.verifier].verify(evidence)

            # Contradicting channels are checked ahead of both the pass and the
            # retry budget.
            #
            # Ahead of the retry budget, so a contradiction can never be repeated
            # even once. See the module docstring for why a retry is the wrong
            # response to one.
            #
            # Ahead of the pass, because a recorded contradiction means the
            # physical state is unknown, and there is no confidence that makes an
            # unknown state green. No agent in AGENTS emits ok=True alongside the
            # record today, so this ordering is not a live path; it is here so
            # that the pair, if it ever arrives, cannot be reported as success.
            # A contradiction fused into a passing number is the exact bug this
            # layer was added to stop.
            #
            # The test is presence of the key, not the shape of its value: the
            # verifier writing the key at all means it saw two channels tell
            # opposite stories, and escalating on a malformed record is the safe
            # direction to be wrong in. The alternative is repeating a motion
            # whose outcome nobody knows.
            data = result.data if isinstance(result.data, dict) else {}
            if "disagreement" in data:
                yield {"step": step.key, "phase": "escalated", "attempt": attempt,
                       "verification": result.__dict__,
                       # Hoisted out of verification.data so a consumer can name
                       # the two channels and the spread without unpacking the
                       # agent's payload.
                       "disagreement": data["disagreement"],
                       "detail": "sensor channels contradict, so the physical state "
                                 "is unknown; not retrying, needs a human"}
                return

            if result.ok:
                yield {"step": step.key, "phase": "passed", "attempt": attempt,
                       "verification": result.__dict__}
                break

            if attempt >= max_attempts:
                yield {"step": step.key, "phase": "failed", "attempt": attempt,
                       "verification": result.__dict__}
                return  # stop the chain; UI surfaces "needs help"
            yield {"step": step.key, "phase": "retrying", "attempt": attempt,
                   "verification": result.__dict__}


def _sampler(step: Step, dm: DeviceManager, out: list[dict[str, Any]]) -> Sampler:
    """A hook that appends one live telemetry snapshot per call.

    Built by a factory rather than closed over the loop variables in ``run``. A
    closure defined in the attempt loop captures the *cell*, not the value, so it
    would not stop working if it outlived its iteration: it would keep appending,
    to whichever ``during`` list the loop is on now. A stale hook would silently
    file its samples against a later attempt, which is worse than raising. The
    factory binds this step and this list once, and that is what makes the hook
    safe to hand to motion code that stores it.

    One call is one ``status()`` read per device in ``step.devices``, on the same
    SDK command lock the motion is using, so sample count is a cost as well as a
    resolution. There is no bench measurement to say what the useful rate is, so
    the choice is left to the caller rather than fixed here.
    """
    def sample() -> None:
        out.append(_telemetry(step, dm))

    return sample


def _execute(step: Step, dm: DeviceManager, sample: Sampler = _no_sample) -> None:
    """Map capability -> driver calls. TODO: fill in taught poses / volumes.

    ``sample`` is the during-trace hook. Call it wherever the motion has just
    made progress the verifier needs to have seen while it was still happening.
    For the unscrew that is once per ratchet increment, because the torque peak
    lives inside that loop and is gone by the time this function returns.

    Cooperative sampling rather than a polling thread, deliberately: the caller
    stays single threaded and testable, and there is no sample rate to invent
    with no bench measurement behind it. The limit of the approach is that one
    blocking driver call cannot be sampled from the inside, so a step needing a
    trace across a single ``arm.move_to`` would need a poller after all. Nothing
    needs that today: cap_removed is the only agent that reads ``during``.
    """
    if step.capability == "dual_arm_manipulation":
        holder: ArmDriver = dm.get(step.params["holding_arm"])   # holds tube
        turner: ArmDriver = dm.get(step.params["turning_arm"])   # turns cap
        # holder.grip(...); turner.grip(...)
        # ratchet-unscrew loop, one sample() per increment while under load:
        #     for _ in range(turns):
        #         turner.<unscrew increment>
        #         sample()
        #
        # One sample until that loop exists. With no motion it records the wrist
        # unloaded, which is true, so the torque channel still says "wrist never
        # loaded" and is right to. It is here so the trace is wired end to end
        # rather than dead, and so the loop above only has to move the call.
        sample()
    elif step.capability in ("arm_transport", "arm_present"):
        arm: ArmDriver = dm.get(step.params["holding_arm"])
        # arm.move_to(Pose(...))
    elif step.capability == "liquid_handling":
        ot: LiquidHandlerDriver = dm.get(step.params["liquid_handler"])
        # ot.aspirate(volume_ul=..., location=DeckLocation(...))


def _frames(dm: DeviceManager) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cam_id in EVIDENCE_CAMERAS:
        try:
            out[cam_id] = dm.get(cam_id).capture()  # type: ignore[attr-defined]
        except Exception:
            pass
    return out


def _telemetry(step: Step, dm: DeviceManager) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dev_id in step.devices:
        try:
            out[dev_id] = dm.get(dev_id).status()
        except Exception:
            pass
    return out
