"""Camera addressed by AVFoundation **uniqueID** — the root-free way to bind a viewpoint
to a physical unit on macOS.

Why not OpenCV. ``cv2.VideoCapture`` takes a device *index*, and an index is not an
identity here: macOS renumbers video devices on any replug, and measured on this bench,
between two consecutive enumerations seconds apart with nothing touched. An index-addressed
slot can silently come up aimed at another camera, or at the operator's built-in one, and
it looks perfectly healthy while doing so. See ``core/cameras.py``.

Why not ffmpeg. Its avfoundation input matches a device *name* correctly, but the two D405
arm cameras share one byte-identical name. Given a uniqueID it does not match it — it
silently opens the default device instead and exits 0, which is worse than an error.

So frames come from ``scripts/avfsnap.swift`` (built on demand into ``temp/bin``), which
calls ``AVCaptureDevice(uniqueID:)`` and fails loudly when there is no such device. It
writes a bare JPEG sequence to stdout; a reader thread here splits it on the JPEG SOI/EOI
markers, exactly as ``remote.py`` does for HTTP MJPEG.

The helper process is a feature, not a wart: a UVC device admits one client, and a crash in
image conversion takes down a subprocess instead of the backend. ``capture_jpeg`` hands the
already-encoded frame straight through, so the common path (MJPEG to the UI) never
decodes.

Config::

    {"unique_id": "0x124300080860b5b", "usb_serial": "351623070085",
     "name": "Gripper cam (right arm)", "width": 1280, "height": 720}

No depth: this is a UVC path, so ``depth_m``/``camera_xyz`` stay null and there are no
factory intrinsics. Metric work still needs librealsense, which needs root on macOS.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.camera import CameraDriver

# Plain stdlib logging: `drivers/` depends on nothing in `core/`.
log = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - opencv is optional at import time
    cv2 = None
    np = None

SOI = b"\xff\xd8"        # JPEG start of image
EOI = b"\xff\xd9"        # JPEG end of image
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Cap the reassembly buffer. A partial frame is at most a frame; anything beyond this means
# the stream is not JPEG at all, and growing without bound would take the backend with it.
MAX_BUFFER = 16 * 1024 * 1024


def _helper_binary() -> str:
    """Path to the avfsnap helper, compiling it on first use.

    Built into temp/ (gitignored) rather than committed: it is machine code for one
    architecture, and swiftc is present on any Mac with the command line tools.
    """
    src = os.path.join(REPO_ROOT, "scripts", "avfsnap.swift")
    out = os.path.join(REPO_ROOT, "temp", "bin", "avfsnap")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    if not os.path.exists(src):
        raise DriverError(f"avfsnap source missing at {src}")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    proc = subprocess.run(["swiftc", "-O", src, "-o", out],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise DriverError(f"could not build avfsnap: {proc.stderr.strip()[:300]}")
    return out


class AVFoundationCameraDriver(CameraDriver):
    """A camera bound to an AVFoundation uniqueID, streamed via the avfsnap helper."""

    has_depth = False

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._frames = 0
        self._started_at = 0.0
        self._last_frame_at = 0.0
        self._stderr_tail = ""

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.CAMERA,
            model=str(self.config.get("model") or "UVC camera"),
            vendor="Intel RealSense (UVC)",
            meta={
                # The identity, surfaced so a wrong-camera suspicion can be checked from
                # the API without shelling into the bench.
                "unique_id": self.config.get("unique_id"),
                "usb_serial": self.config.get("usb_serial"),
                "depth": False,
                "live": True,
            },
        )

    # --- lifecycle ---------------------------------------------------------------
    def connect(self) -> None:
        if cv2 is None:
            raise DriverError("opencv-python not installed")
        unique_id = str(self.config.get("unique_id") or "").strip()
        if not unique_id:
            raise DriverError(f"{self.device_id}: no unique_id configured "
                              "(see core/cameras.py)")
        self._state = ConnectionState.CONNECTING
        self._verify_present(unique_id)

        binary = _helper_binary()
        width = int(self.config.get("width") or 1280)
        height = int(self.config.get("height") or 720)
        quality = int(self.config.get("jpeg_quality") or 85)
        self._stop.clear()
        self._proc = subprocess.Popen(
            [binary, "stream", unique_id, str(width), str(height), str(quality)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self._started_at = time.time()
        self._reader = threading.Thread(target=self._read_frames, name=f"avf-{self.device_id}",
                                        daemon=True)
        self._reader.start()

        # Wait for a real frame before declaring the camera connected: the alternative is a
        # slot that reports healthy and then raises on every capture.
        deadline = time.time() + float(self.config.get("open_timeout_s") or 15.0)
        while time.time() < deadline:
            with self._lock:
                if self._jpeg is not None:
                    self._state = ConnectionState.CONNECTED
                    return
            if self._proc.poll() is not None:
                break
            time.sleep(0.05)

        reason = self._stderr_tail.strip() or "no frame within timeout"
        self.disconnect()
        self._state = ConnectionState.ERROR
        raise DriverError(f"{self.device_id}: {reason}")

    def _verify_present(self, unique_id: str) -> None:
        """Fail with a useful message when this uniqueID is not attached.

        Also warns when the unit behind the port is not the serial we recorded — someone
        moved a camera between ports, and every viewpoint downstream would be mislabelled.
        """
        binary = _helper_binary()
        proc = subprocess.run([binary, "list"], capture_output=True, text=True, check=False)
        rows = [line.split("\t") for line in proc.stdout.splitlines() if "\t" in line]
        ids = {row[0] for row in rows}
        if unique_id not in ids:
            attached = ", ".join(sorted(i for i in ids if i.startswith("0x"))) or "none"
            raise DriverError(
                f"{self.device_id}: no camera at uniqueID {unique_id}. RealSense units "
                f"attached: {attached}. If a camera moved to another USB port, re-run "
                "scripts/identify_cameras.py and update core/cameras.py"
            )
        expected = str(self.config.get("usb_serial") or "")
        if expected:
            actual = _usb_serial_for(unique_id)
            if actual and actual != expected:
                # A warning, not a print: R-CAM-7 requires this to be reachable
                # programmatically (readiness state, log view), not only visible in
                # whichever terminal happened to start the backend.
                log.warning("port %s now holds unit %s, expected %s — the viewpoint may be "
                            "wrong (update core/cameras.py)", unique_id, actual, expected,
                            extra={"device": self.device_id, "event": "camera_event"})

    def disconnect(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        with self._lock:
            self._jpeg = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        alive = self._proc is not None and self._proc.poll() is None
        with self._lock:
            frames, last = self._frames, self._last_frame_at
        elapsed = max(time.time() - self._started_at, 1e-6) if self._started_at else 0.0
        return {
            "state": self._state,
            "connected": alive and self._jpeg is not None,
            "frames": frames,
            "fps": round(frames / elapsed, 1) if elapsed else 0.0,
            "age_s": round(time.time() - last, 2) if last else None,
            "unique_id": self.config.get("unique_id"),
            "usb_serial": self.config.get("usb_serial"),
        }

    # --- frames ------------------------------------------------------------------
    def _read_frames(self) -> None:
        """Split the helper's stdout into JPEGs. Runs on its own thread."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        err_thread = threading.Thread(target=self._read_stderr, daemon=True)
        err_thread.start()
        buf = b""
        while not self._stop.is_set():
            chunk = stdout.read(65536)
            if not chunk:
                break                      # helper exited
            buf += chunk
            # Keep only the newest complete frame: a slow consumer must not build a backlog
            # of stale views of a bench that has since moved.
            while True:
                start = buf.find(SOI)
                if start < 0:
                    break
                end = buf.find(EOI, start + 2)
                if end < 0:
                    break
                frame, buf = buf[start:end + 2], buf[end + 2:]
                with self._lock:
                    self._jpeg = frame
                    self._frames += 1
                    self._last_frame_at = time.time()
            if len(buf) > MAX_BUFFER:
                buf = b""

    def _read_stderr(self) -> None:
        """Keep the last stderr line, so a failure can explain itself."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail = line

    def _latest(self) -> bytes:
        with self._lock:
            jpeg = self._jpeg
        if jpeg is None:
            raise DriverError(f"{self.device_id}: camera not connected")
        return jpeg

    def capture(self) -> Any:
        frame = cv2.imdecode(np.frombuffer(self._latest(), np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise DriverError(f"{self.device_id}: jpeg decode failed")
        return frame

    def capture_jpeg(self, quality: int = 85) -> bytes:
        # Already JPEG from the helper — re-encoding would only lose quality and time. The
        # quality argument is honoured at the helper (jpeg_quality in config).
        return self._latest()


def _usb_serial_for(unique_id: str) -> str | None:
    """USB serial of the unit at the port this uniqueID names, via ioreg.

    A uniqueID's high bytes are the USB locationID: 0x124300080860b5b -> 0x01243000.
    """
    hexpart = unique_id[2:] if unique_id.lower().startswith("0x") else unique_id
    location = hexpart[:4].lower()
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                             capture_output=True, text=True, timeout=30,
                             check=False).stdout
    except Exception:                                            # noqa: BLE001
        return None
    current = None
    for line in out.splitlines():
        if "+-o " in line and "@" in line:
            at = line.split("@")[-1].split()[0].lower()
            current = at.startswith(location)
        elif current and '"USB Serial Number"' in line:
            return line.split('"')[-2]
    return None
