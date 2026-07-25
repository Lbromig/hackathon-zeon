"""REST endpoints for the instrument fleet."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..schemas import ActionResult, DeviceSummary
from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


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
