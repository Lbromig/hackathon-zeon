"""Tests for core.perception.capture (per-camera frame persistence).

Self-contained: synthesises frames, writes into tmp_path, needs no camera.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.perception import camera_dir, load_depth_mm, save_frame, timestamp


def _color(h: int = 8, w: int = 12) -> np.ndarray:
    return np.dstack([np.full((h, w), v, np.uint8) for v in (10, 20, 30)])


@pytest.fixture
def root(tmp_path) -> str:
    """A capture root of its own, rather than tmp_path itself: an autouse conftest fixture
    writes teach_poses.json into tmp_path, which would show up in directory listings."""
    return str(tmp_path / "captures")


def test_each_camera_gets_its_own_subfolder(root):
    save_frame("gripper_cam", _color(), root)
    save_frame("overview_cam", _color(), root)

    assert sorted(os.listdir(root)) == ["gripper_cam", "overview_cam"]
    for cam in ("gripper_cam", "overview_cam"):
        assert "latest_color.png" in os.listdir(os.path.join(root, cam))


def test_saves_accumulate_rather_than_overwrite(root):
    """Distinct stamps must not collide — the folder is a record, not a single slot."""
    save_frame("cam", _color(), root, stamp="20260101T000000_000Z")
    save_frame("cam", _color(), root, stamp="20260101T000001_000Z")

    stamped = [f for f in os.listdir(os.path.join(root, "cam")) if not f.startswith("latest")]
    assert len(stamped) == 2


def test_depth_round_trips_in_metres(root):
    """16-bit millimetre PNG must survive the write/read cycle.

    cv2.imread silently truncates 16-bit to 8-bit unless IMREAD_UNCHANGED is used, which
    would turn a millimetre measurement into noise — load_depth_mm exists to prevent that.
    """
    depth = np.array([[0.0, 0.5], [1.234, 3.0]], np.float32)
    written = save_frame("cam", None, root, depth=depth, stamp="s")

    depth_path = next(p for p in written if p.endswith("_depth.png"))
    back = load_depth_mm(depth_path)
    # Quantisation is 1 mm by construction, so that is the tolerance.
    assert np.allclose(back, depth, atol=1e-3)


def test_colour_is_preserved_exactly(root):
    """PNG is lossless; a switch to JPEG here would quietly corrupt stored evidence."""
    frame = _color()
    written = save_frame("cam", frame, root, stamp="s")
    back = cv2.imread(next(p for p in written if p.endswith("_color.png")))
    assert np.array_equal(back, frame)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "with space", ""])
def test_camera_ids_cannot_escape_the_capture_root(root, bad):
    """Fleet ids are user-editable config, so a slash or .. must not write outside root."""
    path = camera_dir(bad, root)
    assert os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == \
        os.path.realpath(root)
    assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(root)


def test_no_frame_writes_nothing(root):
    assert save_frame("cam", None, root) == []


def test_timestamp_is_sortable_and_filename_safe():
    a, b = timestamp(), timestamp()
    assert a <= b                                    # string sort == chronological
    assert not set(a) & set('/\\:*?"<>| ')
