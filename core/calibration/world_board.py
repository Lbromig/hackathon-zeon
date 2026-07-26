"""The shared world-frame board: tag36h11 ids 210 & 211.

Both fixed cameras (overview + handover) see these two tags, so each camera can solve its
own pose against them (see extrinsics.py) and thereby land in ONE metric world frame.

The board *defines* the world frame:
  - origin at the midpoint between the two tags,
  - +X pointing from tag 210 toward tag 211,
  - the tags coplanar in the world XY plane, facing +Z.
If the physical board is mounted vertically (facing the cameras) rather than flat, change the
tag orientations in `_tag_world_poses` — everything downstream only needs a consistent frame.

`board_object_points()` returns each tag's four corners in world coordinates, in the SAME
order OpenCV's aruco detector returns image corners (so they pair directly for solvePnP).
"""
from __future__ import annotations

import numpy as np

from ..worldmodel.entities import Transform, from_xyz_rpy
from .markers import WORLD_BOARD_IDS

BOARD_TAG_SIZE_M = 0.020        # printed tag size (matches the sticker stock)
# Center-to-center distance between tag 210 and tag 211 on the printed board.
# TODO(measure): set to the real spacing with calipers; this default is a placeholder.
BOARD_SPACING_M = 0.060


def _tag_world_poses() -> dict[int, Transform]:
    """Pose of each board tag's centre in the world frame (origin = board midpoint)."""
    half = BOARD_SPACING_M / 2.0
    lo, hi = sorted(WORLD_BOARD_IDS)[:2]     # e.g. 210, 211
    return {lo: from_xyz_rpy(x=-half), hi: from_xyz_rpy(x=+half)}


def _corner_template(size_m: float) -> np.ndarray:
    """A tag's 4 corners in its own frame, in OpenCV aruco order (TL, TR, BR, BL) —
    identical to core.perception.fiducials so world and image corners pair up."""
    s = size_m / 2.0
    return np.array([[-s, s, 0.0], [s, s, 0.0], [s, -s, 0.0], [-s, -s, 0.0]], float)


def board_object_points(size_m: float = BOARD_TAG_SIZE_M) -> dict[int, np.ndarray]:
    """{tag_id: (4,3) corners in world coordinates} for every board tag."""
    tmpl = np.c_[_corner_template(size_m), np.ones(4)]     # (4,4) homogeneous
    out: dict[int, np.ndarray] = {}
    for tid, T in _tag_world_poses().items():
        out[tid] = (T @ tmpl.T).T[:, :3]
    return out
