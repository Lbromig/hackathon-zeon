"""REST endpoints for the instrument fleet, plus the live `/ws/state` push.

`/ws/state` lived in `api/workflow.py` until the hero workflow was deleted. It has
nothing to do with running a workflow — it is the fleet's own status stream, and the
frontend's `useFleet` composable depends on it — so it moved here rather than dying with
its old neighbours. It gets its own prefix-less router: an APIRouter prefix applies to
websocket routes too, so hanging it off the `/api/instruments` router would silently
rename the path to `/api/instruments/ws/state` and every client would just see a closed
socket.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..schemas import ActionResult, DeviceSummary
from ..services.camera_hub import camera_hub
from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/instruments", tags=["instruments"])
# Prefix-less, for /ws/state (see the module docstring).
state_router = APIRouter(tags=["instruments"])


@router.get("", response_model=list[DeviceSummary])
def list_instruments() -> list[DeviceSummary]:
    return [DeviceSummary(**d) for d in device_manager.snapshot()]


@router.post("/connect", response_model=dict)
def connect_all() -> dict:
    return device_manager.connect_all()


@router.post("/{device_id}/connect", response_model=ActionResult)
def connect(device_id: str) -> ActionResult:
    try:
        device_manager.connect(device_id)
        return ActionResult(ok=True, detail="connected")
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    except Exception as e:
        return ActionResult(ok=False, detail=str(e))


@router.get("/{device_id}/status", response_model=DeviceSummary)
def status(device_id: str) -> DeviceSummary:
    for d in device_manager.snapshot():
        if d["id"] == device_id:
            return DeviceSummary(**d)
    raise HTTPException(404, f"no device {device_id}")


@router.get("/{device_id}/frame")
def frame(device_id: str) -> Response:
    """Latest JPEG frame from a camera driver."""
    try:
        dev = device_manager.get(device_id)
        jpeg = dev.capture_jpeg()  # type: ignore[attr-defined]
    except AttributeError:
        raise HTTPException(400, f"{device_id} is not a camera")
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    except Exception as e:
        raise HTTPException(503, str(e))
    return Response(content=jpeg, media_type="image/jpeg")


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


@state_router.websocket("/ws/state")
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
