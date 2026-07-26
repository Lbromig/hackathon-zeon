"""Calibration/initialization control + the digital-twin snapshot."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from core.calibration import CalibrationPipeline
from core.viz import scene_svg, world_scene

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


@router.get("/api/worldmodel/scene")
def scene() -> dict:
    """Top-down scene (camera + entity world positions) for the world-map view."""
    wm = twin.get_world()
    if wm is None:
        raise HTTPException(409, "not calibrated yet — run /ws/calibrate")
    return world_scene(wm)


@router.get("/api/worldmodel/scene.svg")
def scene_image() -> Response:
    """The same world map rendered server-side as an SVG (openable / embeddable)."""
    wm = twin.get_world()
    if wm is None:
        raise HTTPException(409, "not calibrated yet — run /ws/calibrate")
    return Response(content=scene_svg(world_scene(wm)), media_type="image/svg+xml")
