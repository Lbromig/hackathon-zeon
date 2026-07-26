"""Fixed-camera extrinsics: solve each fixed camera's world pose from the 210/211 board.

This is W2 (fixed cameras only — the on-arm gripper camera's hand-eye is separate and not
done here). With every fixed camera's `T_world_cam` written into the twin, the perception
loop becomes world-correct: fusion composes real world coordinates and projection draws
overlays that sit on the real objects, and the two fixed cameras finally share one frame.

Pure/solver part (`solve_world_cam`) is hardware-free and unit-tested by synthetic
round-trip; `calibrate_fixed_camera` adds detection + writes the twin.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..worldmodel import WorldModel
from ..worldmodel.entities import Transform
from .markers import WORLD_BOARD_IDS
from .world_board import board_object_points

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


@dataclass
class ExtrinsicResult:
    cam_id: str
    ok: bool
    reason: str = ""
    n_board_tags: int = 0
    T_world_cam: Transform | None = None


def solve_world_cam(corners_by_id: dict[int, np.ndarray], K, dist=None,
                    object_points: dict[int, np.ndarray] | None = None) -> Transform | None:
    """Solve T_world_cam from detected board-tag image corners.

    corners_by_id : {tag_id: (4,2) image px} in OpenCV aruco corner order.
    Returns the 4x4 camera pose in the world frame, or None if it can't be solved.
    """
    if cv2 is None:
        return None
    obj_pts = object_points if object_points is not None else board_object_points()
    dist = np.zeros(5) if dist is None else np.asarray(dist, float)

    obj, img = [], []
    for tid, px in corners_by_id.items():
        if tid in obj_pts:
            obj.append(obj_pts[tid])
            img.append(np.asarray(px, float).reshape(4, 2))
    if not obj:
        return None
    obj = np.vstack(obj).astype(np.float64)
    img = np.vstack(img).astype(np.float64)

    # SQPNP handles planar boards and any point count (>=3) without an initial guess.
    ok, rvec, tvec = cv2.solvePnP(obj, img, np.asarray(K, float), dist,
                                  flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    T_cam_world = np.eye(4)
    T_cam_world[:3, :3] = R
    T_cam_world[:3, 3] = tvec.flatten()
    return np.linalg.inv(T_cam_world)          # T_world_cam


def board_corners_in_frame(frame, detector=None) -> dict[int, np.ndarray]:
    """Detect the world-board tags (210/211) in a frame and return their image corners."""
    if detector is None:
        from ..perception.fiducials import FiducialDetector
        detector = FiducialDetector()
    return {d.marker_id: np.asarray(d.corners, float)
            for d in detector.detect(frame) if d.marker_id in WORLD_BOARD_IDS}


def calibrate_fixed_camera(wm: WorldModel, cam_id: str, frame, K, dist=None,
                           detector=None) -> ExtrinsicResult:
    """Detect the board in `frame`, solve the camera pose, and write it into the twin."""
    corners = board_corners_in_frame(frame, detector)
    if not corners:
        return ExtrinsicResult(cam_id, False, "no world-board tags (210/211) in view")
    T = solve_world_cam(corners, K, dist)
    if T is None:
        return ExtrinsicResult(cam_id, False, "solvePnP failed", len(corners))
    if cam_id in wm.entities:
        wm.set_world_pose(cam_id, T)
    return ExtrinsicResult(cam_id, True, "ok", len(corners), T)
