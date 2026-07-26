"""Camera feeds + live detections.

`GET /api/cameras/{id}/stream` is a plain MJPEG multipart response, so the frontend
can point an `<img>` at it — no player, no WebRTC. Detections travel separately as
JSON (here and on `/ws/state`) and the UI draws them as an SVG overlay: video and
overlay stay independently debuggable, and a slow detector never stalls the video.

Frames come from the hub (backend/app/services/camera_hub.py), which owns the one
thread allowed to read each device.
"""
from __future__ import annotations

import json
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from core.config import settings
from drivers import CameraDriver, ConnectionState, InstrumentKind

from ..schemas import CameraDetections, CameraDevices, CameraSummary, RealSenseDevice
from ..services.camera_hub import camera_hub, mjpeg_stream
from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

BOUNDARY = "frame"
REMOTE_TIMEOUT_S = 5.0


def _remote_devices(host: str) -> CameraDevices:
    """`GET {host}/api/cameras/devices`, with failures reported as `error`.

    Same contract as local enumeration: an unreachable bench is a normal state to be
    displayed, not a 500 — and the message carries the URL, because that is the part
    that is usually wrong.
    """
    url = f"{host}/api/cameras/devices"
    try:
        with urllib.request.urlopen(url, timeout=REMOTE_TIMEOUT_S) as resp:
            return CameraDevices(**json.loads(resp.read().decode()))
    except Exception as e:
        return CameraDevices(error=f"cannot reach {url}: {e}")


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
        cfg = dev.config
        out.append(CameraSummary(
            id=dev.device_id, name=dev.info.name, model=dev.info.model,
            state=dev.state.value, connected=dev.state == ConnectionState.CONNECTED,
            streaming=worker is not None,
            width=snap.width if snap else 0,
            height=snap.height if snap else 0,
            fps=snap.fps if snap else 0.0,
            error=snap.error if snap else "",
            has_depth=bool(getattr(dev, "has_depth", False)),
            serial=str(cfg.get("serial") or ""),
            # What the fleet asked for, so the UI can show 1280x720@30 before a
            # single frame exists — the cameras may well be unplugged.
            configured={k: cfg[k] for k in ("width", "height", "fps") if k in cfg},
            intrinsics=snap.intrinsics if snap else None,
        ))
    return out


@router.get("/devices", response_model=CameraDevices)
def devices() -> CameraDevices:
    """Attached RealSense units, for filling in CAM_GRIPPER / CAM_OVERVIEW / CAM_HANDOVER.

    Enumeration is a live SDK call and fails in ordinary ways (SDK absent, no USB
    permission, nothing plugged in) — those are reported as `error`, not raised,
    because "no cameras yet" is the normal state during bring-up.

    Under HZ_CAMERA_HOST it forwards to that backend instead: the frames come from
    there, so the hardware worth enumerating is there too. This is the one camera
    endpoint that needs saying explicitly — the rest are driver-mediated, so the
    remote driver already puts them on the far machine's cameras.
    """
    if settings.camera_host:
        return _remote_devices(settings.camera_host)
    try:
        import pyrealsense2 as rs
    except Exception as e:
        return CameraDevices(error=f"pyrealsense2 unavailable: {e}")

    pinned = {
        str(d.config.get("serial")): d.device_id
        for d in device_manager.all() if d.config.get("serial")
    }
    try:
        found = []
        for dev in rs.context().query_devices():
            serial = dev.get_info(rs.camera_info.serial_number)
            found.append(RealSenseDevice(
                serial=serial,
                name=dev.get_info(rs.camera_info.name),
                firmware=dev.get_info(rs.camera_info.firmware_version),
                assigned_to=pinned.get(serial),
            ))
        if not found:
            return CameraDevices(devices=[], error=_no_devices_reason())
        return CameraDevices(devices=found)
    except Exception as e:
        return CameraDevices(error=str(e))


def _no_devices_reason() -> str:
    """Tell "nothing attached" apart from "attached, but the claim was refused".

    query_devices() returns an EMPTY LIST rather than raising when librealsense
    cannot claim a device. So the SDK's answer is byte-identical whether the bench
    has no camera at all or four that the OS will not release, and the caller
    renders "no cameras" over a rig that is fully populated. Measured on this
    bench: four RealSense units attached, rs-enumerate-devices logging
    RS2_USB_STATUS_ACCESS on every one, and this endpoint reporting an empty list
    with no error.

    Cross-checked against the OS view, which needs no claim to answer.
    """
    try:
        import camera_probe

        attached = [d for d in camera_probe.detect() if d.is_realsense]
    except Exception as e:                      # detection is best-effort
        return f"librealsense enumerated no devices, and the OS view failed too: {e}"
    if not attached:
        # Deliberately not "none are attached". A RealSense that another
        # librealsense process already owns also vanishes from the OS camera list,
        # so this branch cannot tell an unplugged camera from a claimed one.
        return ("librealsense enumerated no devices and the OS lists no RealSense "
                "units either. Most likely nothing is plugged in, though a unit "
                "already owned by another librealsense process looks identical "
                "from here.")
    names = ", ".join(sorted({d.name for d in attached}))
    return (
        f"{len(attached)} RealSense unit(s) are attached ({names}) but librealsense "
        f"claimed none of them, which it reports as zero devices rather than as an "
        f"error. This is an exclusive-ownership problem, not a missing camera. Run "
        f"`python3 camera_probe.py probe` for the specific diagnosis and fix."
    )


@router.post("/{device_id}/connect", response_model=dict)
def connect(device_id: str) -> dict:
    """Try to open the camera. The reason for failure is the useful part here."""
    driver = _camera(device_id)
    if driver.state == ConnectionState.CONNECTED:
        return {"ok": True, "detail": "already connected"}
    try:
        driver.connect()
        return {"ok": True, "detail": f"{device_id} connected"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@router.get("/{device_id}/stream")
def stream(device_id: str) -> StreamingResponse:
    """Live MJPEG. Starts the camera on first viewer; it stops again when idle."""
    driver = _camera(device_id)
    try:
        worker = camera_hub.start(driver)
    except Exception as e:
        # An unplugged camera is not a server fault: answer 503 with the reason.
        # Broad on purpose — vendor SDKs raise their own exception types, and one
        # escaping here would turn "camera offline" into an opaque 500.
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
        fps=snap.fps, error=snap.error, has_depth=snap.has_depth,
        intrinsics=snap.intrinsics,
        detections=[d.as_dict() for d in snap.detections],
    )


@router.post("/{device_id}/stop", response_model=dict)
def stop(device_id: str) -> dict:
    """Release the device without waiting for the idle timeout."""
    _camera(device_id)
    camera_hub.stop(device_id)
    return {"ok": True, "detail": f"{device_id} stream stopped"}
