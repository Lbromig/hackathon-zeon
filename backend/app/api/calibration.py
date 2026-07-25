"""Calibration/initialization control + the digital-twin snapshot."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..calibration import CalibrationPipeline
from ..services import twin
from ..services.device_manager import device_manager

router = APIRouter(tags=["calibration"])


@router.websocket("/ws/calibrate")
async def calibrate(ws: WebSocket) -> None:
    """Run the init+calibration pipeline, streaming a per-step event, then publish the twin."""
    await ws.accept()
    pipeline = CalibrationPipeline(device_manager)
    try:
        # driver calls are blocking -> run the generator off the event loop
        def _steps():
            return list(pipeline.run())

        for event in await asyncio.to_thread(_steps):
            await ws.send_json(event)
        twin.set_world(pipeline.wm)
        await ws.send_json({"phase": "published", "entities": len(pipeline.wm.entities)})
    except WebSocketDisconnect:
        pass


@router.get("/api/worldmodel")
def worldmodel() -> dict:
    wm = twin.get_world()
    if wm is None:
        raise HTTPException(409, "not calibrated yet — run /ws/calibrate")
    return {"entities": wm.snapshot()}
