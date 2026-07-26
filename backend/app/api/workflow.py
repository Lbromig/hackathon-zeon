"""Workflow control + live event stream (websocket)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from core import teach_poses

from ..schemas import PreflightResult, RequiredPose
from ..services.camera_hub import camera_hub
from ..services.device_manager import device_manager
from ..workflows import uncap_aspirate

router = APIRouter(tags=["workflow"])


@router.get("/api/workflow/plan")
def plan() -> list[dict]:
    return [
        {"step": s.key, "capability": s.capability, "devices": s.devices, "verifier": s.verifier}
        for s in uncap_aspirate.PLAN
    ]


@router.get("/api/workflow/required_poses", response_model=list[RequiredPose])
def required_poses() -> list[RequiredPose]:
    """Every taught pose the workflow needs, and whether it exists yet.

    Derived from ``uncap_aspirate.CHOREOGRAPHY`` rather than a hand-maintained list,
    so the teach checklist can never drift from what the workflow actually visits:
    add a waypoint to the choreography and it shows up here as untaught.
    """
    library = teach_poses.load()
    out: list[RequiredPose] = []
    for step in uncap_aspirate.PLAN:
        order = 0
        for act in uncap_aspirate.CHOREOGRAPHY.get(step.key, []):
            if act.kind != "move":
                continue
            order += 1
            entry = library.get(act.device, {}).get(act.pose)
            out.append(RequiredPose(
                device=act.device, name=act.pose, step=step.key, order=order,
                note=act.note, taught=entry is not None,
                saved_at=(entry or {}).get("saved_at", ""),
            ))
    return out


@router.get("/api/workflow/preflight", response_model=PreflightResult)
def workflow_preflight() -> PreflightResult:
    """Exactly the check ``run()`` performs before it moves anything.

    Same function, so a green banner here means the workflow will get past its own
    pre-flight — the UI cannot disagree with the orchestrator about readiness.
    """
    problems = uncap_aspirate.preflight(device_manager)
    return PreflightResult(ok=not problems, problems=problems,
                           required=required_poses())


@router.websocket("/ws/workflow")
async def run_workflow(ws: WebSocket) -> None:
    """Runs the uncap->aspirate workflow, streaming step/verify/retry events."""
    await ws.accept()
    try:
        for event in uncap_aspirate.run(device_manager):
            await ws.send_json(event)
            await asyncio.sleep(0)  # yield to the event loop
        await ws.send_json({"phase": "done"})
    except WebSocketDisconnect:
        pass


_SNAPSHOT_TTL_S = 0.4        # just under the 0.5 s push interval
_snapshot_cache: tuple[float, list[dict]] | None = None
_snapshot_lock = asyncio.Lock()


async def _fleet_snapshot() -> list[dict]:
    """Fleet snapshot, off the event loop and shared between clients.

    `device_manager.snapshot()` does blocking SDK socket reads (get_tcp_pose /
    get_joint_pos per arm, serialized behind the SDK's command lock). Calling it
    directly from a coroutine freezes the event loop for the duration — with a move
    in flight that can be hundreds of ms, during which uvicorn cannot even parse an
    incoming POST /stop. So: run it in the threadpool, and cache it so N websocket
    clients cost one poll rather than N.
    """
    global _snapshot_cache
    async with _snapshot_lock:
        now = asyncio.get_running_loop().time()
        if _snapshot_cache is not None and now - _snapshot_cache[0] < _SNAPSHOT_TTL_S:
            return _snapshot_cache[1]
        data = await run_in_threadpool(device_manager.snapshot)
        _snapshot_cache = (now, data)
        return data


@router.websocket("/ws/state")
async def state_stream(ws: WebSocket) -> None:
    """Pushes a fleet snapshot ~2x/second for live status in the UI.

    The `cameras` block carries the latest detections per running camera. It is a
    plain in-memory read (the hub's worker threads produce it), so unlike the fleet
    snapshot it costs nothing to include on every tick.
    """
    await ws.accept()
    try:
        while True:
            await ws.send_json({
                "instruments": await _fleet_snapshot(),
                "cameras": camera_hub.snapshot(),
            })
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
