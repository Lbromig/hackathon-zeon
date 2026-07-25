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

from ..services.device_manager import DeviceManager
from ..verification.agents import AGENTS, Evidence


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


def run(dm: DeviceManager, *, max_attempts: int = MAX_ATTEMPTS) -> Iterator[dict]:
    for step in PLAN:
        attempt = 0
        while True:
            attempt += 1
            yield {"step": step.key, "phase": "started", "attempt": attempt,
                   "devices": step.devices, "capability": step.capability}

            _execute(step, dm)

            yield {"step": step.key, "phase": "verifying", "attempt": attempt}
            result = AGENTS[step.verifier].verify(_collect_evidence(step, dm))

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
        left: ArmDriver = dm.get("left")   # holds tube
        right: ArmDriver = dm.get("right")  # turns cap
        # left.grip(...); right.grip(...); ratchet-unscrew loop ...
    elif step.capability in ("arm_transport", "arm_present"):
        right: ArmDriver = dm.get("right")
        # right.move_to(Pose(...))
    elif step.capability == "liquid_handling":
        ot: LiquidHandlerDriver = dm.get("ot")
        # ot.aspirate(volume_ul=..., location=DeckLocation(...))


def _collect_evidence(step: Step, dm: DeviceManager) -> Evidence:
    ev = Evidence()
    for cam_id in ("on_arm", "external"):
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
