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

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from drivers import CameraDriver, ConnectionState, InstrumentDriver, InstrumentKind

CAPTURE_FPS = 15.0
DETECT_EVERY = 3          # detect on every Nth frame -> ~5 Hz at 15 fps
JPEG_QUALITY = 80
# Seconds to keep a camera open after the last viewer; <= 0 means "never release".
# Default is never: on macOS, repeatedly opening and closing the same UVC device
# within one process degrades it (frame grabs start failing while a *fresh* process
# still reads the same camera fine). A bench feed is wanted continuously anyway, so
# open once per backend lifetime and let shutdown do the closing.
IDLE_LINGER_S = float(os.getenv("HZ_CAMERA_IDLE_LINGER_S", "0"))
ERROR_BACKOFF_S = 1.0
REOPEN_AFTER_FAILURES = 5   # consecutive grab failures before cycling the device


@dataclass
class Detection:
    """One highlighted thing in the image. Polygon is normalized to [0, 1].

    Metric fields are populated only when the camera can supply them, and stay
    None otherwise rather than being guessed:

    * `distance_m` — from the tag's solvePnP pose; needs intrinsics.
    * `depth_m`    — measured by the depth sensor at the tag centre; RGB-D only.
    * `camera_xyz` — the tag centre in the camera frame (metres), back-projected
      from depth when available, else taken from the tag pose.
    """
    kind: str                       # apriltag | tube | cap | well ...
    polygon: list[list[float]]
    center: list[float]
    source: str = "apriltag"        # apriltag | cv | projection
    marker_id: int | None = None
    entity_id: str | None = None
    confidence: float = 1.0
    distance_m: float | None = None
    depth_m: float | None = None
    camera_xyz: list[float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "polygon": self.polygon, "center": self.center,
            "source": self.source, "marker_id": self.marker_id,
            "entity_id": self.entity_id, "confidence": self.confidence,
            "distance_m": self.distance_m, "depth_m": self.depth_m,
            "camera_xyz": self.camera_xyz,
        }


@dataclass
class CameraSnapshot:
    width: int = 0
    height: int = 0
    seq: int = 0
    fps: float = 0.0
    detections: list[Detection] = field(default_factory=list)
    error: str = ""
    has_depth: bool = False
    intrinsics: dict[str, Any] | None = None   # factory K on RealSense; None otherwise

    def as_dict(self) -> dict[str, Any]:
        return {
            "w": self.width, "h": self.height, "seq": self.seq,
            "fps": round(self.fps, 1), "error": self.error,
            "has_depth": self.has_depth, "intrinsics": self.intrinsics,
            "detections": [d.as_dict() for d in self.detections],
        }


def _detector(camera_matrix: Any = None) -> Any:
    """FiducialDetector, or None when OpenCV/contrib isn't available.

    Imported lazily: the backend must still boot on a machine without cv2, it just
    won't detect anything. Passing the camera matrix is what upgrades detection
    from "here is a quadrilateral" to a full 6-DoF tag pose — which is why a
    RealSense (factory intrinsics, no ChArUco pass) gives distances for free.
    """
    try:
        from core.perception.fiducials import FiducialDetector

        return FiducialDetector(camera_matrix=camera_matrix)
    except Exception as e:  # pragma: no cover - depends on the install
        print(f"[camera_hub] fiducial detection unavailable: {e}")
        return None


def _shape_detector(intrinsics: dict[str, Any] | None = None) -> Any:
    """ShapeDetector (classical CV for un-tagged round labware), or None without cv2."""
    try:
        from core.perception.shapes import ShapeDetector

        return ShapeDetector(intrinsics)
    except Exception as e:  # pragma: no cover - depends on the install
        print(f"[camera_hub] shape detection unavailable: {e}")
        return None


def _intrinsics(driver: CameraDriver) -> dict[str, Any] | None:
    """Pinhole intrinsics {fx, fy, cx, cy, ...} if the camera reports them."""
    try:
        return driver.intrinsics()
    except Exception:
        return None


def _camera_matrix(driver: CameraDriver) -> Any:
    """The driver's 3x3 K, if it has one. Only valid once connected."""
    getter = getattr(driver, "camera_matrix", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None            # e.g. RealSense before the pipeline has started


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

        # Intrinsics only exist after connect(), and the hub connects before
        # starting us, so it is safe to read them here.
        self._intrinsics = _intrinsics(driver)
        self._K = _camera_matrix(driver)
        self._has_depth = bool(getattr(driver, "has_depth", False))
        self._detector = _detector(self._K)
        self._shapes = _shape_detector(self._intrinsics)

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
        try:
            self._pump()
        finally:
            # Hand the device back. A UVC camera admits one owner, and an open
            # VideoCapture survives the thread that made it — so without this the
            # first viewer holds the camera for the life of the process, and every
            # later open (ours or anyone else's) fails with a bare "cannot open".
            try:
                self.driver.disconnect()
            except Exception as e:  # pragma: no cover - teardown must not raise
                print(f"[camera_hub] {self.driver.device_id} disconnect failed: {e}")

    def _pump(self) -> None:
        frame_no = 0
        failures = 0
        detections: list[Detection] = []
        fps_mark, fps_count, fps = time.monotonic(), 0, 0.0

        while not self._stop.is_set():
            started = time.monotonic()
            if self._expired(started):
                break
            try:
                frame, depth = self._grab()
                jpeg = self.driver.capture_jpeg(JPEG_QUALITY) if not _HAS_ENCODER else _encode(frame)
                h, w = frame.shape[:2]
                frame_no += 1
                if frame_no % self.detect_every == 0:
                    detections = self._detect(frame, depth, w, h)
                error = ""
            except Exception as e:
                # Keep the worker alive: a USB camera that hiccups should recover,
                # and the UI needs the reason rather than a dead stream.
                self._publish_error(str(e))
                failures += 1
                # A capture session can break for good (observed: "frame grab
                # failed" for every read after ~5000 frames). Re-reading a dead
                # handle never recovers, so cycle the device instead of spinning
                # on it — the alternative is a feed that is "streaming" forever
                # and delivers nothing.
                if failures % REOPEN_AFTER_FAILURES == 0:
                    self._reopen()
                self._stop.wait(ERROR_BACKOFF_S)
                continue

            failures = 0
            fps_count += 1
            if started - fps_mark >= 1.0:
                fps = fps_count / (started - fps_mark)
                fps_mark, fps_count = started, 0

            with self._new_frame:
                self._jpeg = jpeg
                self._snapshot = CameraSnapshot(
                    width=w, height=h, seq=frame_no, fps=fps,
                    detections=detections, error=error,
                    has_depth=self._has_depth, intrinsics=self._intrinsics,
                )
                self._new_frame.notify_all()

            self._stop.wait(max(0.0, self.period - (time.monotonic() - started)))

    def _reopen(self) -> None:
        """Close and reopen the device after repeated grab failures."""
        print(f"[camera_hub] {self.driver.device_id}: reopening after repeated grab failures")
        try:
            self.driver.disconnect()
        except Exception:
            pass
        try:
            self.driver.connect()
        except Exception as e:
            self._publish_error(f"reopen failed: {e}")

    def _expired(self, now: float) -> bool:
        with self._new_frame:
            if IDLE_LINGER_S <= 0:
                return False          # hold the device for the process lifetime
            idle = self._subscribers == 0 and self._idle_since is not None
            return idle and (now - self._idle_since) > IDLE_LINGER_S

    def _publish_error(self, message: str) -> None:
        with self._new_frame:
            self._snapshot = CameraSnapshot(width=self._snapshot.width,
                                            height=self._snapshot.height,
                                            seq=self._snapshot.seq, error=message,
                                            has_depth=self._has_depth,
                                            intrinsics=self._intrinsics)
            self._new_frame.notify_all()

    def _grab(self) -> tuple[Any, Any]:
        """One frameset: colour, plus aligned depth on an RGB-D camera.

        `capture_rgbd()` takes both from the *same* frameset, so the depth sampled
        under a tag belongs to the frame that tag was detected in. Falling back to
        two separate calls would silently pair mismatched frames.
        """
        if self._has_depth:
            try:
                return self.driver.capture_rgbd()
            except NotImplementedError:
                pass                        # claims depth but doesn't implement it
        return self.driver.capture(), None

    def _detect(self, frame: Any, depth: Any, w: int, h: int) -> list[Detection]:
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
            u, v = d.center
            depth_m = _sample_depth(depth, u, v)
            out.append(Detection(
                kind="apriltag", polygon=polygon,
                center=[u / w, v / h],
                marker_id=d.marker_id, entity_id=d.entity_id,
                distance_m=d.distance_m,
                depth_m=depth_m,
                camera_xyz=self._camera_point(u, v, depth_m, d),
            ))
        out.extend(self._project_twin(w, h))
        out.extend(self._detect_shapes(frame, depth, w, h))
        return out

    def _detect_shapes(self, frame: Any, depth: Any, w: int, h: int) -> list[Detection]:
        """Classical-CV circles for un-tagged labware (source='cv'), depth-back-projected."""
        if self._shapes is None:
            return []
        try:
            shapes = self._shapes.detect(frame, depth)
        except Exception as e:  # a CV fault must not kill the video/detection
            print(f"[camera_hub] {self.driver.device_id} shape detect failed: {e}")
            return []
        return [Detection(**s.as_detection_kwargs()) for s in shapes]

    def _project_twin(self, w: int, h: int) -> list[Detection]:
        """Overlay outlines for calibrated twin entities (source='projection').

        Needs the camera's intrinsics and its pose in the twin; degrades to nothing
        (fiducials still show) when either is missing, e.g. before calibration or on a
        camera that reports no intrinsics.
        """
        if self._K is None:
            return []
        try:
            from core.perception.projection import project_twin

            from . import twin
            wm = twin.get_world()
            if wm is None or self.driver.device_id not in wm.entities:
                return []
            polys = project_twin(wm, self.driver.device_id, self._K, w, h)
        except Exception as e:  # projection must never kill the video/detection
            print(f"[camera_hub] {self.driver.device_id} projection failed: {e}")
            return []
        return [Detection(kind=p["kind"], polygon=p["polygon"], center=p["center"],
                          source="projection", entity_id=p["entity_id"]) for p in polys]

    def _camera_point(self, u: float, v: float, depth_m: float | None,
                      det: Any) -> list[float] | None:
        """Tag centre in camera coordinates (metres), or None if unknowable.

        Depth is preferred over the tag pose: a 20 mm tag subtends few pixels, so
        its solvePnP range is far noisier than a direct depth reading. Falls back
        to the pose translation when there's no depth (or the pixel reads 0, which
        is what a RealSense returns for "no return", not "at the sensor").
        """
        i = self._intrinsics
        if depth_m and i and i.get("fx") and i.get("fy"):
            return [
                round((u - i["cx"]) * depth_m / i["fx"], 4),
                round((v - i["cy"]) * depth_m / i["fy"], 4),
                round(depth_m, 4),
            ]
        T = getattr(det, "T_cam_marker", None)
        if T is not None:
            return [round(float(x), 4) for x in T[:3, 3]]
        return None


def _sample_depth(depth: Any, u: float, v: float, patch: int = 2) -> float | None:
    """Median non-zero depth (metres) in a small patch around (u, v).

    A single pixel on a tag edge often reads 0 (no return). The median over a
    patch, ignoring zeros, is far steadier — and returns None rather than a
    confident 0.0 when the whole patch is invalid.
    """
    if depth is None:
        return None
    try:
        import numpy as np

        h, w = depth.shape[:2]
        cu, cv = int(round(u)), int(round(v))
        if not (0 <= cu < w and 0 <= cv < h):
            return None
        window = depth[max(0, cv - patch):cv + patch + 1, max(0, cu - patch):cu + patch + 1]
        valid = window[np.isfinite(window) & (window > 0)]
        return float(np.median(valid)) if valid.size else None
    except Exception:
        return None


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
