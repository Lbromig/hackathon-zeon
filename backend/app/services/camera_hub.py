"""One frame owner per camera, feeding both the MJPEG stream and the detector.

Why a hub instead of grabbing frames where they are needed: `cv2.VideoCapture` is
not safe to read from two threads, and the UI wants the *same* frame that produced
the overlay. So exactly one worker thread per camera pulls frames and publishes
the latest JPEG plus the latest detections; everyone else reads that snapshot.

Detection runs at a fraction of the capture rate (`detect_every`) so a slow
detector can never stall the video — the plan's decoupling requirement.

Workers are started on demand (the first stream subscriber) and stop again after a
short linger with no subscribers, so the backend does not hold a webcam open just
because it booted.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from drivers import CameraDriver, ConnectionState, InstrumentDriver, InstrumentKind

CAPTURE_FPS = 15.0
DETECT_EVERY = 3          # detect on every Nth frame -> ~5 Hz at 15 fps
JPEG_QUALITY = 80
IDLE_LINGER_S = 10.0      # keep the device open this long after the last viewer
ERROR_BACKOFF_S = 1.0


@dataclass
class Detection:
    """One highlighted thing in the image. Polygon is normalized to [0, 1]."""
    kind: str                       # apriltag | tube | cap | well ...
    polygon: list[list[float]]
    center: list[float]
    source: str = "apriltag"        # apriltag | cv | projection
    marker_id: int | None = None
    entity_id: str | None = None
    confidence: float = 1.0
    distance_m: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "polygon": self.polygon, "center": self.center,
            "source": self.source, "marker_id": self.marker_id,
            "entity_id": self.entity_id, "confidence": self.confidence,
            "distance_m": self.distance_m,
        }


@dataclass
class CameraSnapshot:
    width: int = 0
    height: int = 0
    seq: int = 0
    fps: float = 0.0
    detections: list[Detection] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "w": self.width, "h": self.height, "seq": self.seq,
            "fps": round(self.fps, 1), "error": self.error,
            "detections": [d.as_dict() for d in self.detections],
        }


def _detector() -> Any:
    """FiducialDetector, or None when OpenCV/contrib isn't available.

    Imported lazily: the backend must still boot on a machine without cv2, it just
    won't detect anything.
    """
    try:
        from core.perception.fiducials import FiducialDetector

        return FiducialDetector()
    except Exception as e:  # pragma: no cover - depends on the install
        print(f"[camera_hub] fiducial detection unavailable: {e}")
        return None


class CameraWorker(threading.Thread):
    """Owns one camera: grab -> encode -> (sometimes) detect -> publish."""

    def __init__(self, driver: CameraDriver, *, capture_fps: float = CAPTURE_FPS,
                 detect_every: int = DETECT_EVERY) -> None:
        super().__init__(name=f"camera-{driver.device_id}", daemon=True)
        self.driver = driver
        self.period = 1.0 / max(1.0, capture_fps)
        self.detect_every = max(1, detect_every)

        self._stop = threading.Event()
        self._new_frame = threading.Condition()
        self._jpeg: bytes | None = None
        self._snapshot = CameraSnapshot()
        self._subscribers = 0
        self._idle_since: float | None = time.monotonic()
        self._detector = _detector()

    # --- consumer side -----------------------------------------------------
    @property
    def snapshot(self) -> CameraSnapshot:
        with self._new_frame:
            return self._snapshot

    def acquire(self) -> None:
        with self._new_frame:
            self._subscribers += 1
            self._idle_since = None

    def release(self) -> None:
        with self._new_frame:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0:
                self._idle_since = time.monotonic()

    def wait_for_frame(self, last_seq: int, timeout: float = 2.0) -> tuple[int, bytes] | None:
        """Block until a frame newer than `last_seq` is published."""
        with self._new_frame:
            if self._snapshot.seq <= last_seq:
                self._new_frame.wait(timeout)
            if self._jpeg is None or self._snapshot.seq <= last_seq:
                return None
            return self._snapshot.seq, self._jpeg

    def stop(self) -> None:
        self._stop.set()
        with self._new_frame:
            self._new_frame.notify_all()

    # --- producer side -----------------------------------------------------
    def run(self) -> None:
        frame_no = 0
        detections: list[Detection] = []
        fps_mark, fps_count, fps = time.monotonic(), 0, 0.0

        while not self._stop.is_set():
            started = time.monotonic()
            if self._expired(started):
                break
            try:
                frame = self.driver.capture()
                jpeg = self.driver.capture_jpeg(JPEG_QUALITY) if not _HAS_ENCODER else _encode(frame)
                h, w = frame.shape[:2]
                frame_no += 1
                if frame_no % self.detect_every == 0:
                    detections = self._detect(frame, w, h)
                error = ""
            except Exception as e:
                # Keep the worker alive: a USB camera that hiccups should recover,
                # and the UI needs the reason rather than a dead stream.
                self._publish_error(str(e))
                self._stop.wait(ERROR_BACKOFF_S)
                continue

            fps_count += 1
            if started - fps_mark >= 1.0:
                fps = fps_count / (started - fps_mark)
                fps_mark, fps_count = started, 0

            with self._new_frame:
                self._jpeg = jpeg
                self._snapshot = CameraSnapshot(width=w, height=h, seq=frame_no,
                                                fps=fps, detections=detections, error=error)
                self._new_frame.notify_all()

            self._stop.wait(max(0.0, self.period - (time.monotonic() - started)))

    def _expired(self, now: float) -> bool:
        with self._new_frame:
            idle = self._subscribers == 0 and self._idle_since is not None
            return idle and (now - self._idle_since) > IDLE_LINGER_S

    def _publish_error(self, message: str) -> None:
        with self._new_frame:
            self._snapshot = CameraSnapshot(width=self._snapshot.width,
                                            height=self._snapshot.height,
                                            seq=self._snapshot.seq, error=message)
            self._new_frame.notify_all()

    def _detect(self, frame: Any, w: int, h: int) -> list[Detection]:
        if self._detector is None or not w or not h:
            return []
        try:
            found = self._detector.detect(frame)
        except Exception as e:  # a detector fault must not kill the video
            print(f"[camera_hub] {self.driver.device_id} detect failed: {e}")
            return []
        out = []
        for d in found:
            # Normalize to [0,1] so the frontend can scale the overlay to any size.
            polygon = [[float(x) / w, float(y) / h] for x, y in d.corners]
            out.append(Detection(
                kind="apriltag", polygon=polygon,
                center=[d.center[0] / w, d.center[1] / h],
                marker_id=d.marker_id, entity_id=d.entity_id,
                distance_m=d.distance_m,
            ))
        return out


try:  # encoding here avoids a second capture() inside capture_jpeg()
    import cv2

    _HAS_ENCODER = True

    def _encode(frame: Any) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes()

except Exception:  # pragma: no cover
    _HAS_ENCODER = False

    def _encode(frame: Any) -> bytes:  # pragma: no cover
        raise RuntimeError("opencv not available")


class CameraHub:
    """Registry of running workers, one per camera device id."""

    def __init__(self) -> None:
        self._workers: dict[str, CameraWorker] = {}
        self._guard = threading.Lock()

    def is_camera(self, driver: InstrumentDriver) -> bool:
        return driver.info.kind == InstrumentKind.CAMERA

    def start(self, driver: CameraDriver) -> CameraWorker:
        """Get (or start) the worker for this camera, connecting it if needed."""
        with self._guard:
            worker = self._workers.get(driver.device_id)
            if worker is not None and worker.is_alive():
                return worker
            if driver.state != ConnectionState.CONNECTED:
                driver.connect()          # raises DriverError -> surfaced as 503
            worker = CameraWorker(driver)
            worker.start()
            self._workers[driver.device_id] = worker
            return worker

    def get(self, device_id: str) -> CameraWorker | None:
        worker = self._workers.get(device_id)
        if worker is not None and not worker.is_alive():
            self._workers.pop(device_id, None)
            return None
        return worker

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-camera state for /ws/state — only cameras that are actually running."""
        out = {}
        for device_id in list(self._workers):
            worker = self.get(device_id)
            if worker is not None:
                out[device_id] = worker.snapshot.as_dict()
        return out

    def stop(self, device_id: str) -> None:
        with self._guard:
            worker = self._workers.pop(device_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self) -> None:
        with self._guard:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()


def mjpeg_stream(worker: CameraWorker, boundary: str = "frame") -> Iterator[bytes]:
    """multipart/x-mixed-replace generator. Runs in FastAPI's threadpool."""
    worker.acquire()
    try:
        last_seq = 0
        while True:
            got = worker.wait_for_frame(last_seq)
            if got is None:
                if not worker.is_alive():
                    return
                continue          # timed out waiting; check liveness and retry
            last_seq, jpeg = got
            yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                   f"Content-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"
    finally:
        worker.release()


camera_hub = CameraHub()
