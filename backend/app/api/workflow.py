"""Workflow control + live event stream (websocket)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.device_manager import device_manager
from ..workflows import uncap_aspirate

router = APIRouter(tags=["workflow"])


@router.get("/api/workflow/plan")
def plan() -> list[dict]:
    return [
        {"step": s.key, "capability": s.capability, "devices": s.devices, "verifier": s.verifier}
        for s in uncap_aspirate.PLAN
    ]


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


@router.websocket("/ws/state")
async def state_stream(ws: WebSocket) -> None:
    """Pushes a fleet snapshot ~2x/second for live status in the UI."""
    await ws.accept()
    try:
        while True:
            await ws.send_json({"instruments": device_manager.snapshot()})
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
