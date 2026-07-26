"""HZ_CAMERA_HOST: cameras borrowed from another backend.

Driven against a real HTTP server on a real socket rather than a mocked urlopen. The
driver's whole job is talking multipart MJPEG over a network, so a mock of the transport
would test the mock — in particular it would not catch a frame split across two reads,
which is the normal case on a live stream and the thing most likely to be wrong.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="the remote camera driver decodes JPEG with cv2")

from drivers import DriverError, build_driver              # noqa: E402
from drivers.camera.remote import _next_part               # noqa: E402

WIDTH, HEIGHT = 64, 48
INTRINSICS = {"fx": 600.0, "fy": 600.0, "cx": 32.0, "cy": 24.0,
              "width": WIDTH, "height": HEIGHT}


def _jpeg(value: int = 90) -> bytes:
    """A uniform frame, so a decode can be checked by its mean pixel value."""
    frame = np.full((HEIGHT, WIDTH, 3), value, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


class _Handler(BaseHTTPRequestHandler):
    """The two endpoints the driver uses. `server.row` is the /api/cameras payload."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self) -> None:
        if self.path == "/api/cameras":
            body = json.dumps([self.server.row]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.endswith("/stream"):
            self._stream()
        else:
            self.send_error(404)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        jpeg = _jpeg()
        self.server.frame_bytes = jpeg
        try:
            while not self.server.halt.is_set():
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode() + jpeg + b"\r\n"
                )
                self.wfile.flush()
                self.server.sent.set()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass          # the driver hung up; that is how a stream ends


@pytest.fixture
def bench():
    """A stand-in for the bench backend, on a real port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.halt = threading.Event()
    server.sent = threading.Event()
    server.frame_bytes = b""
    server.row = {"id": "overview_cam", "name": "Overview", "model": "Intel RealSense D435i",
                  "intrinsics": INTRINSICS}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.halt.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def driver(bench):
    d = build_driver({"type": "remote", "id": "overview_cam",
                      "base_url": f"http://127.0.0.1:{bench.server_port}",
                      "name": "Borrowed overview"})
    yield d
    d.disconnect()


# --- the driver ------------------------------------------------------------
def test_frames_arrive_and_decode(driver, bench):
    driver.connect()
    frame = driver.capture()
    assert frame.shape == (HEIGHT, WIDTH, 3)
    assert abs(float(frame.mean()) - 90.0) < 2.0          # JPEG is lossy; the frame is flat


def test_capture_jpeg_passes_the_remote_bytes_through(driver, bench):
    """No re-encode: the far end already compressed this, and doing it twice only loses
    detail. A changed byte here means someone added a decode/encode round trip."""
    driver.connect()
    assert driver.capture_jpeg() == bench.frame_bytes


def test_capture_returns_a_copy(driver):
    """The detector overlay annotates frames in place — two callers must not share one."""
    driver.connect()
    first = driver.capture()
    first[:] = 0
    assert driver.capture().mean() > 1.0


def test_intrinsics_and_camera_matrix_come_from_the_remote(driver):
    driver.connect()
    assert driver.intrinsics() == INTRINSICS
    K = driver.camera_matrix()
    assert K[0][0] == 600.0 and K[0][2] == 32.0 and K[2][2] == 1.0


def test_intrinsics_are_refetched_once_frames_flow(bench):
    """A RealSense reports intrinsics only after its pipeline starts, and our /stream
    request is what starts it — so the first /api/cameras read legitimately says null.
    Without the second read the slot never gets a camera matrix and tag distance is
    silently lost, because the hub reads intrinsics once, right after connect()."""
    bench.row = dict(bench.row, intrinsics=None)

    def serve_intrinsics_after_streaming():
        bench.sent.wait(5.0)
        bench.row = dict(bench.row, intrinsics=INTRINSICS)

    threading.Thread(target=serve_intrinsics_after_streaming, daemon=True).start()
    d = build_driver({"type": "remote", "id": "overview_cam",
                      "base_url": f"http://127.0.0.1:{bench.server_port}"})
    try:
        d.connect()
        assert d.intrinsics() == INTRINSICS
    finally:
        d.disconnect()


def test_no_depth_over_mjpeg(driver):
    """Honest about the capability loss rather than returning a guessed depth."""
    driver.connect()
    assert driver.has_depth is False
    with pytest.raises(DriverError, match="no depth over MJPEG"):
        driver.capture_depth()


def test_info_marks_the_slot_as_a_proxy(driver):
    driver.connect()
    meta = driver.info.meta
    assert meta["live"] is True            # unlike `still`, these ARE live observations
    assert meta["depth"] is False
    assert meta["remote"].endswith("/api/cameras/overview_cam/stream")
    assert "Remote" in driver.info.model


def test_status_reports_frame_age(driver):
    driver.connect()
    status = driver.status()
    assert status["connected"] is True
    assert status["age_s"] is not None and status["age_s"] < 5.0


def test_stalled_stream_is_an_error_not_a_stale_frame(bench):
    """The one failure a verification rig cannot tolerate is a frozen picture presented
    as a current observation. Once frames stop, capture() must say so."""
    d = build_driver({"type": "remote", "id": "overview_cam",
                      "base_url": f"http://127.0.0.1:{bench.server_port}",
                      "stale_after_s": 0.2})
    try:
        d.connect()
        assert d.capture() is not None
        bench.halt.set()                       # far end stops sending
        time.sleep(0.4)
        with pytest.raises(DriverError, match="stalled"):
            d.capture()
    finally:
        d.disconnect()


def test_unreachable_host_names_the_url(bench):
    port = bench.server_port
    bench.halt.set()
    bench.shutdown()
    bench.server_close()
    d = build_driver({"type": "remote", "id": "overview_cam",
                      "base_url": f"http://127.0.0.1:{port}"})
    with pytest.raises(DriverError, match=f"cannot reach http://127.0.0.1:{port}/api/cameras"):
        d.connect()


def test_unknown_remote_id_lists_what_the_remote_has(bench):
    d = build_driver({"type": "remote", "id": "gripper_cam",
                      "base_url": f"http://127.0.0.1:{bench.server_port}"})
    with pytest.raises(DriverError, match="has no camera 'gripper_cam'.*overview_cam"):
        d.connect()


def test_base_url_without_a_scheme(bench):
    d = build_driver({"type": "remote", "id": "overview_cam",
                      "base_url": f"127.0.0.1:{bench.server_port}/"})
    try:
        d.connect()
        assert d.capture() is not None
    finally:
        d.disconnect()


# --- multipart parsing -----------------------------------------------------
def _part(jpeg: bytes, *, with_length: bool = True) -> bytes:
    head = b"--frame\r\nContent-Type: image/jpeg\r\n"
    if with_length:
        head += f"Content-Length: {len(jpeg)}\r\n".encode()
    return head + b"\r\n" + jpeg + b"\r\n"


def test_part_split_across_reads_waits_for_the_rest():
    """The normal case on a live stream: a frame spans several socket reads."""
    jpeg = _jpeg()
    whole = _part(jpeg)
    body, rest, found = _next_part(whole[: len(whole) // 2])
    assert not found and body == b""
    body, rest, found = _next_part(whole)
    assert found and body == jpeg


def test_two_parts_in_one_read():
    a, b = _jpeg(40), _jpeg(200)
    buf = _part(a) + _part(b)
    first, buf, found = _next_part(buf)
    assert found and first == a
    second, buf, found = _next_part(buf)
    assert found and second == b


def test_falls_back_to_jpeg_markers_without_content_length():
    """Third-party MJPEG sources omit the length; ours always sends it."""
    jpeg = _jpeg()
    body, _, found = _next_part(_part(jpeg, with_length=False))
    assert found
    assert body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9")
    assert cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR) is not None
