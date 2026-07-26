"""Camera feed + detection overlay, end to end against synthetic AprilTags.

`mock_tag_camera` renders real tag36h11 markers with the same dictionary the
detector uses, so this exercises the whole chain — capture, JPEG encode, detect,
normalize, serialize — without a webcam. Only the photons are fake.

The MJPEG endpoint itself is driven through the generator rather than TestClient:
an infinite streaming response has no natural end in-process, so a request-based
test would hang rather than fail.
"""
import time

import drivers.mock  # noqa: F401  -- registers the mock driver types
import pytest
from fastapi.testclient import TestClient

cv2 = pytest.importorskip("cv2", reason="opencv-contrib-python is required for tag detection")
pytestmark = pytest.mark.skipif(not hasattr(cv2, "aruco"),
                                reason="cv2.aruco needs opencv-contrib-python, not opencv-python")

FLEET = [
    {"type": "mock_tag_camera", "id": "external", "name": "External cam",
     "markers": [180, 183, 224], "width": 640, "height": 360, "motion": False},
    # Stands in for a RealSense: reports intrinsics + a flat depth plane, which is
    # what turns a tag polygon into metric camera-frame coordinates.
    {"type": "mock_tag_camera", "id": "gripper_cam", "name": "Gripper cam",
     "markers": [224], "width": 640, "height": 360, "motion": False,
     "depth": True, "depth_m": 0.42, "serial": "TESTSERIAL1"},
    {"type": "mock_arm", "id": "left", "name": "Left arm"},
]


@pytest.fixture
def client(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "fleet", FLEET)

    from backend.app.main import app
    from backend.app.services.camera_hub import camera_hub

    with TestClient(app) as c:
        yield c
    camera_hub.stop_all()


def running_worker(client, device_id: str = "external"):
    """Start the hub worker directly and wait until it has published a frame."""
    from backend.app.services.camera_hub import camera_hub
    from backend.app.services.device_manager import device_manager

    worker = camera_hub.start(device_manager.get(device_id))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if worker.snapshot.seq > 0 and worker.snapshot.detections:
            return worker
        time.sleep(0.05)
    raise AssertionError(f"worker produced no detections: {worker.snapshot}")


# --- discovery ---------------------------------------------------------------

def test_lists_only_cameras(client):
    cams = client.get("/api/cameras").json()
    assert [c["id"] for c in cams] == ["external", "gripper_cam"]
    assert cams[0]["streaming"] is False          # nothing has asked for frames yet
    assert client.get("/api/cameras/left/snapshot").status_code == 400   # not a camera
    assert client.get("/api/cameras/nope/snapshot").status_code == 404


def test_summary_exposes_rgbd_configuration(client):
    """The UI must be able to describe a camera that has never delivered a frame."""
    cams = {c["id"]: c for c in client.get("/api/cameras").json()}

    assert cams["gripper_cam"]["has_depth"] is True
    assert cams["gripper_cam"]["serial"] == "TESTSERIAL1"
    assert cams["gripper_cam"]["configured"] == {"width": 640, "height": 360}
    assert cams["external"]["has_depth"] is False


def test_device_discovery_never_500s(client):
    """Enumeration fails in ordinary ways (no SDK, no USB perms, nothing plugged in)."""
    r = client.get("/api/cameras/devices")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["devices"], list)
    # Not "devices or error": with nothing plugged in, an empty list and an empty
    # error is the honest answer, and demanding one of the two be non-empty fails
    # exactly when the cameras are genuinely absent. What matters is that the
    # request returns at all -- it used to segfault the server outright.
    assert isinstance(body["error"], str)


def test_connect_reports_outcome(client):
    body = client.post("/api/cameras/external/connect").json()
    assert body["ok"] is True
    assert client.post("/api/cameras/external/connect").json()["detail"] == "already connected"


def test_snapshot_returns_a_jpeg(client):
    r = client.get("/api/cameras/external/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"           # SOI marker


def test_detections_are_empty_but_ok_when_idle(client):
    body = client.get("/api/cameras/external/detections").json()
    assert body["streaming"] is False and body["detections"] == []


# --- detection ---------------------------------------------------------------

def tags(body) -> list[dict]:
    """Only the fiducial detections — the same frame also carries classical-CV
    shapes and twin projections, which have no marker id by design."""
    return [d for d in body["detections"] if d["source"] == "apriltag"]


def test_detects_the_rendered_tags(client):
    running_worker(client)
    body = client.get("/api/cameras/external/detections").json()

    assert body["streaming"] is True
    assert (body["w"], body["h"]) == (640, 360)
    assert {d["marker_id"] for d in tags(body)} == {180, 183, 224}


def test_markers_resolve_to_twin_entities(client):
    """The marker map is what makes the overlay say 'tube_1_cap' instead of '224'."""
    running_worker(client)
    found = {d["marker_id"]: d["entity_id"] for d in
             client.get("/api/cameras/external/detections").json()["detections"]}
    assert found[224] == "tube_1_cap"
    assert found[180] == "left_base"


def test_polygons_are_normalized(client):
    """The frontend scales these to any rendered size — they must be in [0,1]."""
    running_worker(client)
    for d in tags(client.get("/api/cameras/external/detections").json()):
        assert len(d["polygon"]) == 4
        for x, y in d["polygon"]:
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        assert 0.0 <= d["center"][0] <= 1.0 and 0.0 <= d["center"][1] <= 1.0


def test_no_metric_data_without_intrinsics_or_depth(client):
    """Honest nulls on a colour-only camera, rather than plausible-looking guesses."""
    running_worker(client)
    body = client.get("/api/cameras/external/detections").json()

    assert body["has_depth"] is False and body["intrinsics"] is None
    for d in tags(body):
        assert d["distance_m"] is None
        assert d["depth_m"] is None
        assert d["camera_xyz"] is None


# --- RGB-D (the RealSense contract) -------------------------------------------

def test_rgbd_camera_publishes_factory_intrinsics(client):
    """A RealSense reports K itself, so tag pose needs no ChArUco calibration pass."""
    running_worker(client, "gripper_cam")
    body = client.get("/api/cameras/gripper_cam/detections").json()

    assert body["has_depth"] is True
    assert set(body["intrinsics"]) >= {"fx", "fy", "cx", "cy", "width", "height"}
    assert body["intrinsics"]["cx"] == pytest.approx(320, abs=1)   # 640 wide, centred


def test_tags_carry_measured_depth_and_camera_coordinates(client):
    """Depth + intrinsics turn a polygon into metres in the camera frame."""
    running_worker(client, "gripper_cam")
    dets = tags(client.get("/api/cameras/gripper_cam/detections").json())
    assert dets

    for d in dets:
        assert d["depth_m"] == pytest.approx(0.42, abs=0.01)       # the mock's plane
        x, y, z = d["camera_xyz"]
        assert z == pytest.approx(0.42, abs=0.01)                  # Z is the depth
        assert abs(x) < 0.5 and abs(y) < 0.5                       # near the optical axis
        assert d["distance_m"] is not None                         # PnP pose too, via K


def test_depth_is_preferred_over_pose_range(client):
    """Back-projection must use the depth reading, not the noisier tag-pose range."""
    running_worker(client, "gripper_cam")
    d = tags(client.get("/api/cameras/gripper_cam/detections").json())[0]

    assert d["camera_xyz"][2] == pytest.approx(d["depth_m"], abs=1e-3)
    assert d["camera_xyz"][2] != pytest.approx(d["distance_m"], abs=1e-3)


# --- stream ------------------------------------------------------------------

def test_mjpeg_generator_emits_multipart_frames(client):
    from backend.app.services.camera_hub import mjpeg_stream

    worker = running_worker(client)
    gen = mjpeg_stream(worker)
    try:
        chunks = [next(gen) for _ in range(3)]
    finally:
        gen.close()

    for chunk in chunks:
        assert chunk.startswith(b"--frame")
        assert b"Content-Type: image/jpeg" in chunk
        assert b"\xff\xd8" in chunk               # a real JPEG rides in the body


def test_stream_subscribers_are_released_on_close(client):
    """A leaked subscriber count would keep the webcam open forever."""
    from backend.app.services.camera_hub import mjpeg_stream

    worker = running_worker(client)
    gen = mjpeg_stream(worker)
    next(gen)
    assert worker._subscribers == 1
    gen.close()
    assert worker._subscribers == 0


def test_stop_releases_the_worker(client):
    from backend.app.services.camera_hub import camera_hub

    running_worker(client)
    assert client.post("/api/cameras/external/stop").json()["ok"] is True
    deadline = time.monotonic() + 3
    while camera_hub.get("external") is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert camera_hub.get("external") is None
    assert client.get("/api/cameras/external/detections").json()["streaming"] is False


# --- websocket ---------------------------------------------------------------

def test_ws_state_carries_camera_detections(client):
    running_worker(client)
    with client.websocket_connect("/ws/state") as ws:
        msg = ws.receive_json()

    assert "instruments" in msg and "cameras" in msg
    external = msg["cameras"]["external"]
    assert external["w"] == 640
    assert {d["entity_id"] for d in external["detections"]} >= {"tube_1_cap", "left_base"}
