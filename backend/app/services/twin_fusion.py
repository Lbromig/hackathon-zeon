"""Background loop that folds live camera detections into the digital twin.

Reads each running camera worker's latest detections (from camera_hub) and, for any
tagged detection that resolves to a twin entity, updates that entity's world position via
core.perception.fusion.TwinFuser. The camera's own world pose comes from the twin, so
this only produces correct world coordinates once calibration has placed the cameras —
until then it still runs (in the camera frame) so the whole pipeline is exercisable.

Kinematics remains the always-on pose source for arm/gantry parents; this is the slower
*corrective* feed (FR-WM). One lock serializes twin writes.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from core.perception import TwinFuser
from drivers import InstrumentKind

from . import twin
from .camera_hub import camera_hub
from .device_manager import device_manager

FUSE_HZ = 10.0


def _camera_ids() -> list[str]:
    return [d.device_id for d in device_manager.all() if d.info.kind == InstrumentKind.CAMERA]


def fuse_once(fuser: TwinFuser) -> list[dict[str, Any]]:
    """One fusion pass over whatever cameras are currently streaming. Returns the
    accepted updates (for logging / a debug endpoint)."""
    wm = twin.get_world()
    if wm is None:
        return []
    out: list[dict[str, Any]] = []
    with wm.lock:                       # one atomic fusion pass over the twin
        for cam_id in _camera_ids():
            worker = camera_hub.get(cam_id)
            if worker is None:
                continue
            for d in worker.snapshot.detections:
                if not d.entity_id or d.camera_xyz is None:
                    continue
                r = fuser.fuse_point(wm, cam_id, d.entity_id, d.camera_xyz,
                                     confidence=d.confidence)
                if r.ok:
                    out.append({"cam": cam_id, "entity": r.entity_id,
                                "world_xyz": r.world_xyz, "marker_id": d.marker_id})
    return out


class TwinFusion:
    """Owns the fusion thread. Started/stopped from the app lifespan."""

    def __init__(self, hz: float = FUSE_HZ) -> None:
        self.period = 1.0 / max(1.0, hz)
        self._fuser = TwinFuser()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="twin-fusion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.last = fuse_once(self._fuser)
            except Exception as e:  # never let fusion kill the app
                print(f"[twin_fusion] {e}")
            self._stop.wait(self.period)


twin_fusion = TwinFusion()
