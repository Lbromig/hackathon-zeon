"""Camera preflight, streaming and snapshot endpoints.

The verification agents in `core/verification` are only trustworthy when a
camera was genuinely readable at the time they ran. This exposes that check to
the UI, so an operator can see before starting a workflow whether the sensors
those agents depend on are actually available, and can watch the feed while a
step runs.

Three transports are offered because they fail differently:

- `/preflight` is the gate. It opens nothing permanently and always answers.
- MJPEG streaming is NOT duplicated here. `api/cameras.py` already serves
  `/api/cameras/{id}/stream` from the hub; this module reuses that reader.
- `/webrtc/offer` is WebRTC via aiortc, for low latency once the feed matters
  more than simplicity. aiortc is an optional dependency and its absence is
  reported rather than raised.

The probe logic lives in `camera_probe.py` at the repo root on purpose. It is
stdlib only and has to keep working when the backend does not, since "the app is
broken" and "the camera is unplugged" need to stay separable.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from ..schemas import (
    CameraPreflight,
    CameraPreflightBackend,
    CameraPreflightDevice,
    CameraSnapshot,
    WebRTCAnswer,
    WebRTCOffer,
)

router = APIRouter(prefix="/api/camera", tags=["camera"])

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE_PATH = _REPO_ROOT / "camera_probe.py"
_SNAPSHOT_DIR = _REPO_ROOT / "snapshots"

# Every active WebRTC peer connection, so they can be closed on shutdown. A
# dropped browser tab leaves the connection behind otherwise, and each one holds
# a capture handle the next client then cannot get.
_peers: set[Any] = set()


def _load_probe() -> Any | None:
    """Import camera_probe.py by path.

    It is a standalone script at the repo root, not part of a package, so a
    normal import will not find it. Returning None rather than raising keeps a
    missing probe from taking down the whole API.
    """
    if "camera_probe" in sys.modules:
        return sys.modules["camera_probe"]
    if not _PROBE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("camera_probe", _PROBE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["camera_probe"] = module
    spec.loader.exec_module(module)
    return module


class FrameSource:
    """A single shared reader for one camera.

    A camera admits exactly one reader. Several viewers, a snapshot and a
    WebRTC track all want frames at once, so one thread owns the device and
    everyone else reads the latest frame it published. Without this the second
    viewer to connect simply fails to open the device.
    """

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self._lock = threading.Lock()
        self._cap: Any = None
        self._latest: Any = None
        self._error: str = ""

    @property
    def error(self) -> str:
        return self._error

    def _ensure_open(self) -> bool:
        """Open the device once. Returns False with `error` set on failure."""
        if self._cap is not None:
            return True
        try:
            import cv2
        except ImportError:
            self._error = "cv2 is not installed in the backend interpreter."
            return False

        # Do not hardcode a device index anywhere durable: this camera
        # re-enumerates and its index and uniqueID both move when it does.
        cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            cap.release()
            probe = _load_probe()
            if probe is not None:
                result = probe.probe_opencv(probe.detect())
                self._error = f"{result.diagnosis.value}: {result.detail}"
            else:
                self._error = "camera failed to open"
            return False
        self._cap = cap
        self._error = ""
        return True

    def read(self) -> Any | None:
        """Latest BGR frame, or None with `error` set."""
        with self._lock:
            if not self._ensure_open():
                return None
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self._error = "device opened but returned no frame"
                return None
            self._latest = frame
            return frame

    def jpeg(self, quality: int = 80) -> bytes | None:
        frame = self.read()
        if frame is None:
            return None
        import cv2

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def close(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None


_source = FrameSource()


@router.get("/preflight", response_model=CameraPreflight)
def preflight() -> CameraPreflight:
    """Enumerate cameras and report whether any capture path actually works.

    This opens streams, so it is a GET with real side effects on device state.
    That is deliberate: the only honest way to report that capture works is to
    capture. It is fast, and read-only with respect to the robot.
    """
    probe = _load_probe()
    if probe is None:
        return CameraPreflight(
            usable=False,
            responsible_app="unknown",
            devices=[],
            backends=[
                CameraPreflightBackend(
                    backend="probe",
                    diagnosis="backend_missing",
                    detail=f"camera_probe.py not found at {_PROBE_PATH}",
                    remedy="Restore camera_probe.py at the repository root.",
                )
            ],
        )

    devices = probe.detect()
    results = [probe.probe_realsense(devices), probe.probe_opencv(devices)]

    return CameraPreflight(
        usable=any(r.ok for r in results),
        responsible_app=probe.responsible_app(),
        devices=[
            CameraPreflightDevice(
                name=d.name,
                model=d.model or d.name,
                serial=d.serial,
                vendor_id=d.vendor_id,
                product_id=d.product_id,
                link_speed_bps=d.link_speed_bps,
                is_realsense=d.is_realsense,
                is_usb3=d.is_usb3,
                claimed_by=sorted(set(d.claimed_by)),
            )
            for d in devices
        ],
        backends=[
            CameraPreflightBackend(
                backend=r.backend,
                diagnosis=r.diagnosis.value,
                detail=r.detail,
                remedy="" if r.ok else probe.REMEDY[r.diagnosis],
            )
            for r in results
        ],
    )


def _hub_jpeg() -> bytes | None:
    """Latest JPEG from a running camera_hub worker, if one is streaming.

    MJPEG streaming already belongs to `api/cameras.py` and `services/camera_hub`,
    which owns the single thread allowed to read each device. Snapshots and
    WebRTC therefore go through the hub whenever it is running rather than
    opening a second reader, since the device admits only one and the second
    caller is the one that fails.
    """
    try:
        from ..services.camera_hub import camera_hub
    except ImportError:
        return None
    for worker in getattr(camera_hub, "_workers", {}).values():
        got = worker.wait_for_frame(last_seq=-1, timeout=2.0)
        if got is not None:
            return got[1]
    return None


def _frame_bytes(quality: int = 92) -> bytes | None:
    """A JPEG from the hub if it is streaming, else a direct one-shot read.

    The fallback exists so the preflight and a snapshot still work before any
    camera driver is registered with the fleet, which is exactly the situation
    during bring-up.
    """
    return _hub_jpeg() or _source.jpeg(quality=quality)


@router.post("/snapshot", response_model=CameraSnapshot)
def snapshot(label: str = "") -> CameraSnapshot:
    """Write one frame to disk, timestamped, and return where it went.

    Snapshots are how a run becomes reviewable after the fact. A verification
    agent's verdict is only auditable if the frame it judged was kept.
    """
    jpeg = _frame_bytes(quality=92)
    if jpeg is None:
        raise HTTPException(503, _source.error or "no camera available")

    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in label if c.isalnum() or c in "-_")[:40]
    name = f"{stamp}{'-' + safe if safe else ''}.jpg"
    path = _SNAPSHOT_DIR / name
    path.write_bytes(jpeg)

    return CameraSnapshot(
        name=name,
        path=str(path.relative_to(_REPO_ROOT)),
        bytes=len(jpeg),
        label=label,
    )


@router.get("/snapshots", response_model=list[CameraSnapshot])
def snapshots() -> list[CameraSnapshot]:
    """Snapshots taken so far, newest first."""
    if not _SNAPSHOT_DIR.exists():
        return []
    files = sorted(_SNAPSHOT_DIR.glob("*.jpg"), reverse=True)
    return [
        CameraSnapshot(
            name=f.name, path=str(f.relative_to(_REPO_ROOT)), bytes=f.stat().st_size
        )
        for f in files[:50]
    ]


@router.post("/webrtc/offer", response_model=WebRTCAnswer)
async def webrtc_offer(offer: WebRTCOffer) -> WebRTCAnswer:
    """Answer a browser WebRTC offer with a live video track.

    Lower latency than MJPEG and it survives packet loss, at the cost of an
    optional native dependency and a signalling round trip. Absence of aiortc is
    reported as a 503 with the install command rather than a 500.
    """
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
        from av import VideoFrame
    except ImportError:
        raise HTTPException(
            503,
            "aiortc is not installed. `uv add aiortc av` to enable WebRTC, or use "
            "/api/camera/stream, which needs no extra dependency.",
        )

    if _frame_bytes() is None:
        raise HTTPException(503, _source.error or "no camera available")

    class CameraTrack(VideoStreamTrack):
        """Publishes frames from the shared FrameSource onto a WebRTC track."""

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            # The capture read is blocking, so it goes to a worker thread. Doing
            # it inline stalls the event loop and every other connection with it.
            frame = await asyncio.to_thread(_source.read)
            if frame is None:
                raise ConnectionError(_source.error or "camera stopped delivering")
            video = VideoFrame.from_ndarray(frame, format="bgr24")
            video.pts, video.time_base = pts, time_base
            return video

    pc = RTCPeerConnection()
    _peers.add(pc)

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            _peers.discard(pc)

    pc.addTrack(CameraTrack())
    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return WebRTCAnswer(sdp=pc.localDescription.sdp, type=pc.localDescription.type)


async def shutdown() -> None:
    """Close every peer connection and release the device."""
    for pc in list(_peers):
        await pc.close()
    _peers.clear()
    _source.close()
