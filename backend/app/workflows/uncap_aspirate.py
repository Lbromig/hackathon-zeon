"""The hero workflow: cooperative uncap -> transport -> aspirate, verified.

Written as a generator of WorkflowStepEvent so the API can stream progress
(and retries) to the UI over websocket. Orchestration is the "middle layer":
it maps each step's required capability to concrete drivers, runs the motion,
then calls the matching verification agent and retries on failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from drivers import ArmDriver, DeckLocation, LiquidHandlerDriver, Pose

from core.config import GRASP_WIDTH_M, HOLDING_ARM, TURNING_ARM
from core.verification.agents import AGENTS, Evidence

from ..services.device_manager import DeviceManager

# Cameras to grab evidence frames from. These must track core.config's fleet ids:
# the previous list ("on_arm", "external") predated the three-camera rig, so every
# lookup missed and the verifiers silently ran with no vision channel at all.
EVIDENCE_CAMERAS = ("gripper_cam", "overview_cam", "handover_cam")


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
        plan: list[Step] | None = None) -> Iterator[dict]:
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

            _execute(step, dm)

            yield {"step": step.key, "phase": "verifying", "attempt": attempt}
            evidence = Evidence(
                frames=_frames(dm),
                telemetry=_telemetry(step, dm),
                before=before,
                before_frames=before_frames,
                expected=dict(step.params),
            )
            result = AGENTS[step.verifier].verify(evidence)

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


def _execute(step: Step, dm: DeviceManager) -> None:
    """Map capability -> driver calls. TODO: fill in taught poses / volumes."""
    if step.capability == "dual_arm_manipulation":
        holder: ArmDriver = dm.get(step.params["holding_arm"])   # holds tube
        turner: ArmDriver = dm.get(step.params["turning_arm"])   # turns cap
        # holder.grip(...); turner.grip(...); ratchet-unscrew loop ...
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
