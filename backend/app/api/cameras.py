"""Camera feeds + live detections.

`GET /api/cameras/{id}/stream` is a plain MJPEG multipart response, so the frontend
can point an `<img>` at it — no player, no WebRTC. Detections travel separately as
JSON (here and on `/ws/state`) and the UI draws them as an SVG overlay: video and
overlay stay independently debuggable, and a slow detector never stalls the video.

Frames come from the hub (backend/app/services/camera_hub.py), which owns the one
thread allowed to read each device.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from drivers import CameraDriver, ConnectionState, DriverError, InstrumentKind

from ..schemas import CameraDetections, CameraSummary
from ..services.camera_hub import camera_hub, mjpeg_stream
from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

BOUNDARY = "frame"


def _camera(device_id: str) -> CameraDriver:
    try:
        dev = device_manager.get(device_id)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    if dev.info.kind != InstrumentKind.CAMERA:
        raise HTTPException(400, f"{device_id} is not a camera")
    return dev  # type: ignore[return-value]


@router.get("", response_model=list[CameraSummary])
def list_cameras() -> list[CameraSummary]:
    out = []
    for dev in device_manager.all():
        if dev.info.kind != InstrumentKind.CAMERA:
            continue
        worker = camera_hub.get(dev.device_id)
        snap = worker.snapshot if worker else None
        out.append(CameraSummary(
            id=dev.device_id, name=dev.info.name, model=dev.info.model,
            state=dev.state.value, connected=dev.state == ConnectionState.CONNECTED,
            streaming=worker is not None,
            width=snap.width if snap else 0,
            height=snap.height if snap else 0,
            fps=snap.fps if snap else 0.0,
            error=snap.error if snap else "",
        ))
    return out


@router.get("/{device_id}/stream")
def stream(device_id: str) -> StreamingResponse:
    """Live MJPEG. Starts the camera on first viewer; it stops again when idle."""
    driver = _camera(device_id)
    try:
        worker = camera_hub.start(driver)
    except DriverError as e:
        raise HTTPException(503, str(e))
    return StreamingResponse(
        mjpeg_stream(worker, BOUNDARY),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/{device_id}/snapshot")
def snapshot(device_id: str) -> Response:
    """A single JPEG — cheaper than the stream for thumbnails and debugging."""
    driver = _camera(device_id)
    worker = camera_hub.get(device_id)
    if worker is not None:
        got = worker.wait_for_frame(0, timeout=2.0)
        if got is not None:
            return Response(content=got[1], media_type="image/jpeg")
    try:
        if driver.state != ConnectionState.CONNECTED:
            driver.connect()
        return Response(content=driver.capture_jpeg(), media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(503, str(e))


@router.get("/{device_id}/detections", response_model=CameraDetections)
def detections(device_id: str) -> CameraDetections:
    """Latest detections for one camera. Empty (not an error) when it isn't running."""
    _camera(device_id)
    worker = camera_hub.get(device_id)
    if worker is None:
        return CameraDetections(id=device_id, streaming=False)
    snap = worker.snapshot
    return CameraDetections(
        id=device_id, streaming=True, w=snap.width, h=snap.height, seq=snap.seq,
        fps=snap.fps, error=snap.error,
        detections=[d.as_dict() for d in snap.detections],
    )


@router.post("/{device_id}/stop", response_model=dict)
def stop(device_id: str) -> dict:
    """Release the device without waiting for the idle timeout."""
    _camera(device_id)
    camera_hub.stop(device_id)
    return {"ok": True, "detail": f"{device_id} stream stopped"}
