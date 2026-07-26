"""Project calibrated twin entities into a camera image → overlay polygons (FR).

The twin knows where things are (world poses) and how big they are (CAD `dims`). Given a
camera's pose in the world (`world_pose(cam_entity_id)`) and its intrinsics `K`, we can draw
each known entity straight onto its video — no per-frame detection needed. This is the
*reliable* half of the overlay: as soon as the space is calibrated (and the fusion loop keeps
poses fresh), highlights sit on the real objects. Detection then only has to handle the
un-modelled / un-tagged things.

Camera convention: the camera entity's frame is the optical frame — +Z forward (into the
scene), +X right, +Y down — matching OpenCV, so a point in front has positive z.

Pure numpy (no OpenCV): projection + a small convex hull, so it unit-tests without cameras.
Output polygons are normalized to [0, 1] and clamped, ready for the SVG overlay.
"""
from __future__ import annotations

import numpy as np

from ..worldmodel import EntityKind, WorldModel

# Kinds worth drawing by default — keep it to the few the demo hinges on so the overlay
# stays cheap and readable (not all 96 tip sites).
DEFAULT_KINDS = (EntityKind.TUBE, EntityKind.CAP, EntityKind.PIPETTE_CHANNEL)

# Fallback half-extent (m) for entities that carry no dims (e.g. a bare nozzle).
_DEFAULT_RADIUS = 0.008
_DEFAULT_HEIGHT = 0.010


def _cylinder_points(radius: float, height: float, n: int = 16) -> np.ndarray:
    """A ring of points at the base and top of the entity's bounding cylinder."""
    a = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    ring = np.c_[radius * np.cos(a), radius * np.sin(a), np.zeros(n)]
    return np.vstack([ring, ring + (0, 0, height)])


def _convex_hull(pts: np.ndarray) -> np.ndarray:
    """Monotone-chain convex hull of (N,2) points, CCW, no external deps."""
    uniq = sorted({(float(x), float(y)) for x, y in pts})
    if len(uniq) <= 2:
        return np.array(uniq, float)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], float)


def project_entity(
    wm: WorldModel, cam_entity_id: str, entity_id: str, K, w: int, h: int, *, n: int = 16
) -> dict | None:
    """Project one entity into the camera. Returns an overlay dict or None if it's not
    in front of the camera / off-frame / unknown."""
    if entity_id not in wm.entities or cam_entity_id not in wm.entities or not w or not h:
        return None
    K = np.asarray(K, float)
    ent = wm.get(entity_id)
    r = 0.5 * ent.dims.get("diameter", 2 * _DEFAULT_RADIUS)
    ht = ent.dims.get("height", _DEFAULT_HEIGHT)

    model = _cylinder_points(r, ht, n)                       # (2n,3) in entity frame
    T_cam_world = np.linalg.inv(wm.world_pose(cam_entity_id))
    T_cam_ent = T_cam_world @ wm.world_pose(entity_id)
    pts_h = np.c_[model, np.ones(len(model))]
    P = (T_cam_ent @ pts_h.T).T[:, :3]                       # entity points in camera frame

    P = P[P[:, 2] > 1e-4]                                    # keep points in front of camera
    if len(P) < 3:
        return None
    uv = (K @ (P / P[:, 2:3]).T).T[:, :2]                    # pinhole projection
    hull = _convex_hull(uv)
    if len(hull) < 3:
        return None

    poly = np.clip(hull / (w, h), 0.0, 1.0)                  # normalize + clamp to frame
    center = np.clip(uv.mean(0) / (w, h), 0.0, 1.0)
    return {
        "entity_id": entity_id,
        "kind": ent.kind.value,
        "source": "projection",
        "polygon": [[round(float(x), 4), round(float(y), 4)] for x, y in poly],
        "center": [round(float(center[0]), 4), round(float(center[1]), 4)],
    }


def project_twin(
    wm: WorldModel, cam_entity_id: str, K, w: int, h: int, *, kinds=DEFAULT_KINDS
) -> list[dict]:
    """Project every entity of the given kinds currently in the twin."""
    out: list[dict] = []
    for kind in kinds:
        for ent in wm.by_kind(kind):
            got = project_entity(wm, cam_entity_id, ent.id, K, w, h)
            if got is not None:
                out.append(got)
    return out
