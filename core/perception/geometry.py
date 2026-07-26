"""Pure rigid-body geometry — the only part of the deleted world model worth keeping.

Salvaged from `core/worldmodel/entities.py` when the digital twin was removed. Nothing
here knows about a scene graph, an entity or a parent frame: it is 4x4 homogeneous
matrices and nothing else, which is exactly what the fiducial detector's `solvePnP`
result is and what the tag-pose offset solve (R-VIS-12) needs.

Units are **metres** and **radians**, matching what `cv2.solvePnP` returns for an object
model expressed in metres. The mm/deg conversion belongs at the action boundary, not here.
"""
from __future__ import annotations

import numpy as np

Transform = np.ndarray  # 4x4 homogeneous matrix, camera<-marker unless stated otherwise


def identity() -> Transform:
    return np.eye(4)


def from_xyz_rpy(x=0.0, y=0.0, z=0.0, roll=0.0, pitch=0.0, yaw=0.0) -> Transform:
    """Pose from translation (m) + intrinsic RPY (rad)."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    R = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = (x, y, z)
    return T


def invert(T: Transform) -> Transform:
    """Inverse of a rigid transform, computed as [R' | -R'@t] rather than via
    `np.linalg.inv`. Same answer for a well-formed pose, but it cannot amplify the
    numerical noise a general inverse can, and it fails loudly on a non-rigid input
    instead of quietly returning a nonsense matrix."""
    T = np.asarray(T, np.float64)
    R, t = T[:3, :3], T[:3, 3]
    out = np.eye(4)
    out[:3, :3] = R.T
    out[:3, 3] = -R.T @ t
    return out


def translation(T: Transform) -> np.ndarray:
    """The translation column, in metres."""
    return np.asarray(T, np.float64)[:3, 3].copy()
