"""Serve another backend's camera over the network, as if it were local hardware.

For a second machine (a laptop, a clone of this repo) that has no cameras plugged in
but needs the bench's viewpoints. Selected wholesale by ``HZ_CAMERA_HOST`` — see
``core/config.py`` — which rewrites every camera slot in the fleet to this driver.

Why this sits at the *driver* layer rather than proxying ``/api/cameras/*`` at the API
layer: everything downstream of a camera reads frames through a driver — the hub's
worker, the fiducial and shape detectors, twin fusion, the world model. An API-level
proxy would put pictures in the Cameras tab while leaving all of that blind. Here the
frames enter at the same place local ones do, so the whole stack works unchanged and
the local backend re-serves its own MJPEG to its own frontend (no CORS, no second
host in the browser's URLs).

Config: {"base_url": "http://bench:8100", "remote_id": "overview_cam", "name": ...}

Two honest limitations, both consequences of the transport:

* **No depth.** MJPEG carries colour only, so ``has_depth`` is False even when the
  remote slot is an RGB-D RealSense. Tag *distance* still works (it comes from
  solvePnP against the intrinsics we fetch); depth-measured ``depth_m`` and
  ``camera_xyz`` do not.
* **Frames are re-detected locally.** We consume pixels, not the remote's
  detections, so detection runs twice — once there, once here. That is deliberate:
  one pipeline, one set of results, no version skew between two backends.

Unlike ``still.py`` this IS a live observation, so ``info.meta`` reports
``live: True``. A frozen stream is reported as an error rather than served on as a
current frame — see ``_fresh_frame``.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

import numpy as np

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.camera import CameraDriver

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

# A frame older than this means the stream died without the socket noticing (the far
# backend was killed, the camera was unplugged there, the Wi-Fi dropped). Raising
# makes the hub show the reason and cycle the connection; serving the last frame on
# would present a stale picture as a current observation, which is the one failure a
# verification rig cannot tolerate.
STALE_AFTER_S = 5.0
FIRST_FRAME_TIMEOUT_S = 10.0     # generous: the far end opens the device on our request
HTTP_TIMEOUT_S = 5.0             # for the JSON calls, and per socket read on the stream
RETRY_BACKOFF_S = 1.0
MAX_PART_BYTES = 32 * 1024 * 1024   # a multipart part larger than this is a desync


class RemoteCameraDriver(CameraDriver):
    """A camera whose frames come from another backend's MJPEG endpoint."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._base = _normalize_base(str(self.config.get("base_url") or ""))
        self._remote_id = str(self.config.get("remote_id") or device_id)
        self._stale_after = float(self.config.get("stale_after_s", STALE_AFTER_S))

        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        # The open stream response, so disconnect() can close the socket out from under
        # a blocked read. Its own lock: `_lock` is held while a frame is being decoded,
        # and shutdown must not queue behind that.
        self._conn_lock = threading.Lock()
        self._conn: Any = None
        self._frame: Any = None
        self._jpeg: bytes | None = None
        self._frame_at: float = 0.0
        self._error: str = ""
        self._intr: dict[str, Any] | None = None
        self._remote_model: str = ""

    # --- identity ----------------------------------------------------------
    @property
    def stream_url(self) -> str:
        return f"{self._base}/api/cameras/{quote(self._remote_id)}/stream"

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.CAMERA,
            model=f"Remote ({self._remote_model})" if self._remote_model else "Remote camera",
            vendor="proxy",
            # `remote` is what tells an operator why a feed exists on a machine with
            # nothing plugged into it; `depth: False` is a real capability loss, not
            # a detail — see the module docstring.
            meta={"remote": self.stream_url, "remote_id": self._remote_id,
                  "live": True, "depth": False},
        )

    # --- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        if cv2 is None:
            raise DriverError("opencv not installed")
        if not self._base:
            raise DriverError("no base_url configured (set HZ_CAMERA_HOST)")

        self._state = ConnectionState.CONNECTING
        # Ask what the far end has before streaming: a typo'd host, a backend that
        # isn't up, or an id that doesn't exist there are all ordinary mistakes, and
        # each has a different fix. Finding out here names the actual problem instead
        # of timing out on a stream that was never going to arrive.
        summary = self._fetch_summary()
        self._remote_model = str(summary.get("model") or "")
        self._intr = summary.get("intrinsics")

        self._stop.clear()
        self._reader = threading.Thread(target=self._pump, name=f"remote-cam-{self.device_id}",
                                        daemon=True)
        self._reader.start()

        if not self._wait_for_first_frame():
            self.disconnect()
            reason = self._error or f"no frame within {FIRST_FRAME_TIMEOUT_S:g}s"
            raise DriverError(f"{self.stream_url}: {reason}")

        # Intrinsics again, now that frames are flowing: a RealSense reports them only
        # once its pipeline is running, and our /stream request is what started it. At
        # connect time above the row legitimately said null. Without this second read
        # the slot never gets a camera matrix, and tag *distance* is silently lost —
        # the hub reads intrinsics once, right after connect() returns.
        if self._intr is None:
            try:
                self._intr = self._fetch_summary().get("intrinsics")
            except DriverError:
                pass                        # frames are what matter; poses can wait
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._stop.set()
        # Close the socket rather than waiting for the read to time out: the reader is
        # parked in resp.read() for up to HTTP_TIMEOUT_S, and joining on that would make
        # every shutdown (and every hub _reopen) stall for seconds per camera.
        self._close_conn()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        with self._lock:
            self._frame = None
            self._jpeg = None
            self._frame_at = 0.0
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = (time.monotonic() - self._frame_at) if self._frame_at else None
            return {
                "state": self._state, "connected": self._frame is not None,
                "remote": self.stream_url, "remote_id": self._remote_id,
                "live": True, "depth": False,
                "age_s": None if age is None else round(age, 2),
                "error": self._error,
            }

    # --- frames ------------------------------------------------------------
    def capture(self) -> Any:
        frame, _ = self._fresh_frame()
        # A copy per call: the detector overlay annotates frames in place, and handing
        # out the cached array would let one consumer scribble on every later read.
        return frame.copy()

    def capture_jpeg(self, quality: int = 85) -> bytes:
        """The bytes the far end sent, verbatim.

        `quality` is ignored on purpose: the frame arrives already JPEG-compressed, and
        re-encoding it would spend CPU to lose more detail. Callers that need a
        specific quality should re-encode `capture()` themselves.
        """
        _, jpeg = self._fresh_frame()
        return jpeg

    def capture_depth(self) -> Any:
        raise DriverError(
            f"{self.device_id}: no depth over MJPEG — this slot proxies "
            f"{self.stream_url}, which carries colour only"
        )

    def intrinsics(self) -> dict[str, Any] | None:
        return dict(self._intr) if self._intr else None

    def camera_matrix(self) -> Any:
        """3x3 K for solvePnP / FiducialDetector, from the remote's intrinsics."""
        i = self._intr
        if not i:
            raise DriverError(f"{self.device_id}: remote reports no intrinsics")
        return np.array([[i["fx"], 0, i["cx"]], [0, i["fy"], i["cy"]], [0, 0, 1]], float)

    def _fresh_frame(self) -> tuple[Any, bytes]:
        with self._lock:
            frame, jpeg, at, error = self._frame, self._jpeg, self._frame_at, self._error
        if frame is None or jpeg is None:
            raise DriverError(f"no frame from {self.stream_url}: {error or 'not connected'}")
        age = time.monotonic() - at
        if age > self._stale_after:
            raise DriverError(
                f"{self.stream_url}: stream stalled, last frame {age:.1f}s ago"
                + (f" ({error})" if error else "")
            )
        return frame, jpeg

    # --- reader thread -----------------------------------------------------
    def _pump(self) -> None:
        """Hold the MJPEG stream open, decoding into the latest-frame slot.

        Reconnects on its own: this is a network camera, and a link that drops for a
        second should not need the hub to cycle the whole device.
        """
        while not self._stop.is_set():
            try:
                self._read_stream()
            except Exception as e:
                self._set_error(str(e))
            if not self._stop.is_set():
                self._stop.wait(RETRY_BACKOFF_S)

    def _read_stream(self) -> None:
        req = urllib.request.Request(self.stream_url, headers={"Accept": "multipart/x-mixed-replace"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            self._set_error("")
            buf = b""
            while not self._stop.is_set():
                chunk = resp.read(65536)
                if not chunk:
                    raise DriverError("stream closed by the remote backend")
                buf += chunk
                buf = self._drain(buf)
                if len(buf) > MAX_PART_BYTES:
                    raise DriverError("multipart desync (no frame boundary found)")

    def _drain(self, buf: bytes) -> bytes:
        """Pull every complete JPEG out of `buf`, returning the unconsumed tail."""
        while True:
            jpeg, buf, found = _next_part(buf)
            if not found:
                return buf
            if jpeg:
                self._publish(jpeg)

    def _publish(self, jpeg: bytes) -> None:
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            self._set_error("received a part that would not decode as JPEG")
            return
        with self._lock:
            self._frame = frame
            self._jpeg = jpeg
            self._frame_at = time.monotonic()
            self._error = ""

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message

    def _wait_for_first_frame(self) -> bool:
        deadline = time.monotonic() + FIRST_FRAME_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                if self._frame is not None:
                    return True
            if self._stop.wait(0.1):
                return False
        return False

    # --- remote metadata ---------------------------------------------------
    def _fetch_summary(self) -> dict[str, Any]:
        url = f"{self._base}/api/cameras"
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
                rows = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise DriverError(f"{url} returned HTTP {e.code}") from e
        except Exception as e:
            # Covers a wrong host, a backend that isn't running, DNS, and TLS alike —
            # the message carries the URL, which is the part that is usually wrong.
            raise DriverError(f"cannot reach {url}: {e}") from e

        if not isinstance(rows, list):
            raise DriverError(f"{url}: expected a list of cameras, got {type(rows).__name__}")
        for row in rows:
            if isinstance(row, dict) and row.get("id") == self._remote_id:
                return row
        have = ", ".join(str(r.get("id")) for r in rows if isinstance(r, dict)) or "none"
        raise DriverError(f"{url} has no camera {self._remote_id!r} (it has: {have})")


def _normalize_base(value: str) -> str:
    """Accept `bench:8100`, `http://bench:8100`, or a trailing slash, all the same."""
    value = value.strip().rstrip("/")
    if value and "://" not in value:
        value = f"http://{value}"
    return value


def _next_part(buf: bytes) -> tuple[bytes, bytes, bool]:
    """Split one multipart part off the front of `buf`.

    Returns (jpeg, remaining, found). `found` is False when more bytes are needed.

    Prefers the part's Content-Length (which this backend's own `mjpeg_stream` always
    sends) and falls back to scanning for the JPEG end-of-image marker, so a
    third-party MJPEG source that omits the length still works. Scanning is the
    fallback rather than the rule because 0xFFD9 can legitimately occur inside
    entropy-coded data — with a length, no guessing is involved.
    """
    head_end = buf.find(b"\r\n\r\n")
    if head_end == -1:
        return b"", buf, False
    headers = buf[:head_end].decode("latin-1", "replace")
    body_at = head_end + 4

    length = _content_length(headers)
    if length is not None:
        if len(buf) < body_at + length:
            return b"", buf, False
        return buf[body_at:body_at + length], buf[body_at + length:], True

    start = buf.find(b"\xff\xd8", body_at)
    if start == -1:
        return b"", buf, False
    end = buf.find(b"\xff\xd9", start + 2)
    if end == -1:
        return b"", buf, False
    return buf[start:end + 2], buf[end + 2:], True


def _content_length(headers: str) -> int | None:
    for line in headers.split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None
